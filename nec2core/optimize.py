"""Coordinate-descent geometry optimiser for NEC-2 decks.

The optimiser treats a card deck as a template: every tunable dimension is a
``SY`` symbol (or, for decks without symbols, an element length/position taken
from the ``GW`` cards).  Candidate values are rendered into a fresh deck, the
NEC engine is run, the output is parsed with :mod:`nec2core.output` and scored
with :mod:`nec2core.metrics`.

Search strategy: a pattern search (coordinate descent).  Each variable is
probed both ways by its current step, the best improvement is accepted, and
the steps are halved when no probe improves the objective.  The evaluation
counter is a hard cap, so ``max_evals`` engine runs are never exceeded.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass

from .engine import NecEngine
from .metrics import summarize_block
from .model import ModelError, eval_expr, has_expressions, parse_deck
from .output import parse_output
from .yagi import YagiDesign

__all__ = ["optimize_design", "apply_variables", "OBJECTIVES"]

OBJECTIVES = ("gain", "fb", "swr", "score")

_INF = float("inf")
_NEG_INF = float("-inf")

# ``SY NAME=expr`` / ``SY NAME expr`` (the inline comment after ' needs to survive).
_SY_RE = re.compile(
    r"^(?P<head>\s*SY\s+)(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?P<sep>\s*=\s*|\s+)(?P<expr>[^\s']+)(?P<tail>.*)$",
    re.IGNORECASE,
)

# Fixed-point formatting for GW coordinate fields.  NEC-2 reads cards as
# 80-column records, so the fields must stay short: 6 decimals (1 um) keeps a
# full GW card well below that limit while preserving the geometry.
_COORD = "{:.6f}"


@dataclass
class _Var:
    """One normalised search variable."""

    name: str
    kind: str  # "symbol" | "element_length" | "element_position" | "yagi"
    lo: float
    hi: float
    step: float
    index: int | None = None
    field: str | None = None
    initial: float | None = None


# ---------------------------------------------------------------------------
# Variable normalisation
# ---------------------------------------------------------------------------
def _to_float(value: object, default: float | None = None) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _normalise(variables: list[dict]) -> list[_Var]:
    """Turn user variable dictionaries into :class:`_Var` objects."""
    if not variables:
        raise ValueError("variables must not be empty")
    result: list[_Var] = []
    for raw in variables:
        if not isinstance(raw, dict):
            raise ValueError("every variable must be a dict")
        kind = str(raw.get("kind") or "").strip().lower()
        symbol = raw.get("symbol")
        if symbol:
            kind = "symbol"
        if not kind:
            raise ValueError(f"variable {raw!r} needs a 'symbol' or a 'kind'")
        if kind not in ("symbol", "element_length", "element_position",
                        "yagi", "yagi_element_length", "yagi_element_position"):
            raise ValueError(f"unsupported variable kind {kind!r}")

        index = raw.get("index")
        field = raw.get("field")
        if kind in ("element_length", "element_position",
                    "yagi_element_length", "yagi_element_position"):
            if index is None:
                raise ValueError(f"variable {raw!r} needs an 'index' (1 = reflector)")
            index = int(index)
            if index < 1:
                raise ValueError(f"variable {raw!r}: 'index' is 1-based")
        if kind == "yagi":
            if not field:
                raise ValueError("a 'yagi' variable needs a 'field' such as 'radius' or 'length:3'")
            field = str(field)

        if raw.get("value") is not None:
            fixed = _to_float(raw.get("value"))
            if fixed is None:
                raise ValueError(f"variable {raw!r}: 'value' is not a number")
            lo = hi = fixed
        else:
            lo = _to_float(raw.get("min"))
            hi = _to_float(raw.get("max"))
            if lo is None or hi is None:
                raise ValueError(f"variable {raw!r} needs 'min' and 'max' (or a fixed 'value')")
        if hi < lo:
            lo, hi = hi, lo
        span = hi - lo
        step = _to_float(raw.get("step"), span / 8.0 if span > 0 else 0.0)
        if step is None or step <= 0:
            step = span / 8.0 if span > 0 else 0.0
        if span > 0:
            step = min(step, span)
        name = str(raw.get("name") or symbol or (f"{kind}_{index}" if index else f"{kind}_{field}" if field else kind))
        result.append(
            _Var(
                name=name,
                kind=kind,
                lo=lo,
                hi=hi,
                step=step,
                index=index,
                field=field,
                initial=_to_float(raw.get("initial")),
            )
        )
    return result


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
# Deck rendering
# ---------------------------------------------------------------------------
def _split_line(line: str) -> tuple[str, list[str], str]:
    """Split a card line into (leading whitespace, tokens, trailing comment)."""
    stripped = line.strip()
    tokens = stripped.split()
    comment = ""
    for i, token in enumerate(tokens):
        if token.startswith("'"):
            comment = " ".join(tokens[i:])
            tokens = tokens[:i]
            break
    indent = line[: len(line) - len(line.lstrip())]
    return indent, tokens, comment


def _insert_symbol(deck: str, name: str, value: float) -> str:
    """Insert ``SY name=value`` before the first data card of the deck."""
    lines = deck.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    new_line = f"SY {name}={value:.9g}"
    comment_labels = {"CM", "CE", "CT", "CK"}
    insert_at = len(lines)
    for i, line in enumerate(lines):
        label = line[:2].upper()
        if not line.strip() or label in comment_labels:
            continue
        insert_at = i
        break
    lines.insert(insert_at, new_line)
    return "\n".join(lines)


def _set_symbol(deck: str, name: str, value: float) -> str:
    """Replace (or add) one ``SY`` symbol in the deck."""
    lines = deck.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    replaced = False
    out: list[str] = []
    for line in lines:
        match = _SY_RE.match(line)
        if match and match.group("name").upper() == name.upper():
            out.append(f"{match.group('head')}{match.group('name')}={value:.9g}{match.group('tail')}")
            replaced = True
        else:
            out.append(line)
    text = "\n".join(out)
    if not replaced:
        text = _insert_symbol(text, name, value)
    return text


def _wire_indices(model) -> list:
    """Return the GW cards in deck order (one entry per cable/element)."""
    return list(model.wires)


def _element_wires(model, index: int) -> list:
    """All wires belonging to element ``index`` (1-based tag, else by position)."""
    by_tag = [w for w in model.wires if w.tag == index]
    if by_tag:
        return by_tag
    wires = _wire_indices(model)
    if 1 <= index <= len(wires):
        return [wires[index - 1]]
    raise ValueError(f"element index {index} is out of range (deck has {len(wires)} wires)")


def _length_axis(wire) -> str:
    deltas = {"x": abs(wire.x2 - wire.x1), "y": abs(wire.y2 - wire.y1), "z": abs(wire.z2 - wire.z1)}
    axis = max(deltas, key=deltas.get)
    if deltas[axis] < 1e-9:
        raise ValueError(f"wire tag {wire.tag} has zero length; cannot scale it")
    return axis


def _position_axis(model, length_axis: str) -> str:
    """Pick the axis the elements are distributed along (the boom axis)."""
    centres = [
        {"x": w.center[0], "y": w.center[1], "z": w.center[2]}
        for w in model.wires
    ]
    spread = {axis: (max(c[axis] for c in centres) - min(c[axis] for c in centres))
              for axis in "xyz" if axis != length_axis}
    axis = max(spread, key=spread.get) if spread else "z"
    if spread and spread[axis] < 1e-9:
        return {"x": "x", "y": "y"}.get(length_axis, "z")
    return axis


def _rewrite_wire_line(line: str, coords: list[float], radius: str) -> str:
    """Rebuild one GW card line with new coordinates, keeping its label/radius."""
    indent, tokens, comment = _split_line(line)
    label = tokens[0]
    tag, segs = tokens[1], tokens[2]
    body = " ".join(
        [label, tag, segs] + [_COORD.format(c) for c in coords] + [radius]
    )
    return f"{indent}{body}" + (f"  {comment}" if comment else "")


def _radius_token(line: str) -> str:
    """Return the radius field of a GW card verbatim (it may be a symbol)."""
    _, tokens, _ = _split_line(line)
    return tokens[9] if len(tokens) > 9 else "0.004"


def _set_element_length(deck: str, index: int, length: float) -> str:
    """Scale element ``index`` to ``length`` about its centre (straight wire)."""
    model = parse_deck(deck)
    wires = _element_wires(model, index)
    if len(wires) != 1:
        raise ValueError(f"element {index} consists of {len(wires)} wires; only straight elements can be scaled")
    wire = wires[0]
    axis = _length_axis(wire)
    coords = [wire.x1, wire.y1, wire.z1, wire.x2, wire.y2, wire.z2]
    axis_pos = {"x": (0, 3), "y": (1, 4), "z": (2, 5)}[axis]
    centre = sum(coords[i] for i in axis_pos) / 2.0
    for i, sign in zip(axis_pos, (-1.0, 1.0)):
        coords[i] = centre + sign * length / 2.0

    lines = deck.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    target = wire.line_no
    lines[target - 1] = _rewrite_wire_line(lines[target - 1], coords, _radius_token(lines[target - 1]))
    return "\n".join(lines)


def _set_element_position(deck: str, index: int, position: float) -> str:
    """Move element ``index`` so its centre sits at ``position`` m on the boom."""
    model = parse_deck(deck)
    wires = _element_wires(model, index)
    if len(wires) != 1:
        raise ValueError(f"element {index} consists of {len(wires)} wires; cannot move it as a unit")
    wire = wires[0]
    length_axis = _length_axis(wire)
    axis = _position_axis(model, length_axis)
    coords = [wire.x1, wire.y1, wire.z1, wire.x2, wire.y2, wire.z2]
    axis_index = {"x": (0, 3), "y": (1, 4), "z": (2, 5)}[axis]
    centre = sum(coords[i] for i in axis_index) / 2.0
    shift = position - centre
    for i in axis_index:
        coords[i] += shift

    lines = deck.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines[wire.line_no - 1] = _rewrite_wire_line(
        lines[wire.line_no - 1], coords, _radius_token(lines[wire.line_no - 1])
    )
    return "\n".join(lines)


def _field_index(var: _Var) -> int:
    """Resolve the element index of a Yagi variable (``index`` or ``field``)."""
    if var.index:
        return int(var.index)
    field = var.field or ""
    if ":" in field:
        return int(field.split(":", 1)[1])
    raise ValueError(f"yagi variable {var.name!r} needs an element 'index'")


def _apply_yagi(design: YagiDesign, var: _Var, value: float) -> None:
    """Apply one YagiDesign-level change to a rebuilt design."""
    field = var.field or ""
    if var.kind == "yagi_element_length" or field.startswith("length"):
        idx = _field_index(var)
        if not (1 <= idx <= len(design.elements)):
            raise ValueError(f"element index {idx} is out of range for the parsed Yagi")
        design.elements[idx - 1].length_m = value
    elif var.kind == "yagi_element_position" or field.startswith("position"):
        idx = _field_index(var)
        if not (1 <= idx <= len(design.elements)):
            raise ValueError(f"element index {idx} is out of range for the parsed Yagi")
        design.elements[idx - 1].position_m = value
    elif field in ("radius", "radius_m"):
        design.radius_m = value
    elif field in ("frequency", "freq_mhz", "freq"):
        design.freq_mhz = value
    elif field in ("boom", "boom_length", "boom_length_m"):
        base = design.elements[0].position_m
        count = len(design.elements)
        for i, element in enumerate(design.elements):
            element.position_m = base + value * i / max(1, count - 1)
    elif field in ("segments", "segments_per_element"):
        design.segments = max(3, int(round(value)) | 1)
    else:
        raise ValueError(f"unsupported yagi field {field!r}")


def _number_token(value: float) -> str:
    """Format an evaluated field for a card line (short, 6 significant digits)."""
    if value == 0.0:
        return "0"
    return f"{value:.6g}"


def _expand_symbols(deck: str) -> str:
    """Replace every ``SY`` reference with its numeric value.

    NEC-2 itself has no ``SY`` card (the 4NEC2 deck files rely on 4NEC2's
    front-end to expand them), so the engine is only ever given a numeric deck.
    Expression fields (``-Hr``, ``Ld1/2``) are evaluated with
    :func:`nec2core.model.eval_expr`; tokens that are plain numbers or cannot be
    evaluated are copied through unchanged.
    """
    symbols = dict(parse_deck(deck).symbols)
    lines: list[str] = []
    for line in deck.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = line.strip()
        label = stripped[:2].upper()
        if not stripped:
            continue
        if label in ("CM", "CE", "CK", "CT", "EN"):
            lines.append(line.rstrip())
            continue
        if label == "SY":
            continue  # the value now lives in the cards that used it
        indent, tokens, comment = _split_line(line)
        if not tokens:
            continue
        expanded = [tokens[0]]
        for token in tokens[1:]:
            try:
                expanded.append(_number_token(eval_expr(token, symbols)))
            except ModelError:
                expanded.append(token)
        lines.append(f"{indent}{' '.join(expanded)}" + (f"  {comment}" if comment else ""))
    return "\n".join(lines) + "\n"


def apply_variables(deck: str, values: dict[str, float], variables: list[dict]) -> str:
    """Render ``values`` into ``deck`` and return the resulting card deck.

    Args:
        deck: the template deck (``SY``-parameterised or plain geometry).
        values: mapping from variable name to its current numeric value.
        variables: the same variable descriptions passed to :func:`optimize_design`.
    """
    normalised = _normalise(variables)
    text = deck

    yagi_vars = [var for var in normalised if var.kind.startswith("yagi")]
    if yagi_vars:
        design = YagiDesign.from_nec(text)
        # Element lengths before positions: moving an element does not depend
        # on its length, but keeping the order deterministic helps the report.
        ordered = sorted(
            yagi_vars,
            key=lambda v: (
                0 if (v.kind == "yagi_element_length" or (v.field or "").startswith("length")) else
                1 if (v.kind == "yagi_element_position" or (v.field or "").startswith("position")) else
                2 if (v.field or "") in ("boom", "boom_length", "boom_length_m") else 3,
                v.index or 0,
            ),
        )
        for var in ordered:
            if var.name in values:
                _apply_yagi(design, var, values[var.name])
        text = design.to_nec()

    for var in normalised:
        if var.kind == "element_length" and var.name in values:
            text = _set_element_length(text, var.index or 1, float(values[var.name]))
        elif var.kind == "element_position" and var.name in values:
            text = _set_element_position(text, var.index or 1, float(values[var.name]))

    for var in normalised:
        if var.kind == "symbol" and var.name in values:
            text = _set_symbol(text, var.name, float(values[var.name]))
    if has_expressions(text):
        text = _expand_symbols(text)
    return text


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def _finite(value: float | None) -> bool:
    return value is not None and math.isfinite(float(value))


def _fb_db(metrics: dict) -> float | None:
    """Front-to-back from the summary, falling back to the best pattern cut."""
    fb = metrics.get("fb_db")
    if _finite(fb):
        return float(fb)
    cuts = metrics.get("cuts") or []
    candidates = [c.get("fb_db") for c in cuts if _finite(c.get("fb_db"))]
    return max(float(c) for c in candidates) if candidates else None


def _natural_score(metrics: dict, objective: str) -> float | None:
    """Return the objective's natural value (dB, SWR or composite score)."""
    gain = metrics.get("gain_max_db")
    fb = _fb_db(metrics)
    swr_value = metrics.get("swr")
    if objective == "gain":
        return float(gain) if _finite(gain) else None
    if objective == "fb":
        return float(fb) if _finite(fb) else None
    if objective == "swr":
        return float(swr_value) if _finite(swr_value) else None
    if objective == "score":
        if not _finite(gain):
            return None
        gain = float(gain)
        swr_value = float(swr_value) if _finite(swr_value) else 99.0
        fb_value = float(fb) if _finite(fb) else 0.0
        return gain - 10.0 * max(0.0, swr_value - 1.5) - 2.0 * max(0.0, 1.0 - fb_value / 15.0)
    raise ValueError(f"unknown objective {objective!r}; choose one of {', '.join(OBJECTIVES)}")


def _fitness(natural: float | None, objective: str) -> float:
    """Map a natural objective value onto a maximisation fitness."""
    if natural is None:
        return _NEG_INF
    return -natural if objective == "swr" else natural


def _json_number(value: float, fallback: float | None = None) -> float | None:
    if not math.isfinite(value):
        return fallback
    return round(value, 6)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def optimize_design(
    deck: str,
    variables: list[dict],
    objective: str = "score",
    engine: NecEngine | None = None,
    max_evals: int = 40,
    z0: float = 50.0,
    timeout_s: float = 240.0,
) -> dict:
    """Tune a deck with a bounded pattern search over ``variables``.

    Args:
        deck: NEC-2 card deck acting as the template.  ``SY`` symbols are
            rewritten in place; decks without symbols can use
            ``{"kind": "element_length", ...}`` variables instead.
        variables: list of dicts, each with ``symbol`` or ``kind`` plus
            ``min``/``max``/``step`` (see :mod:`nec2core.optimize` docs).
        objective: ``"gain"`` (maximise), ``"fb"`` (maximise front-to-back),
            ``"swr"`` (minimise) or ``"score"`` (gain/directivity trade-off).
        engine: an initialised :class:`nec2core.engine.NecEngine`; a default
            one is created when omitted.
        max_evals: hard cap on engine runs.
        z0: feed-line impedance used for SWR.
        timeout_s: wall-clock budget for the whole optimisation.

    Returns:
        ``{"best": {name: value}, "score": float, "metrics": dict,
        "evaluations": int, "history": [...], ...}`` where ``score`` is the
        objective's natural value (SWR for the ``"swr"`` objective) and
        ``fitness`` is the internal maximised value.
    """
    objective = str(objective or "score").strip().lower()
    if objective not in OBJECTIVES:
        raise ValueError(f"unknown objective {objective!r}; choose one of {', '.join(OBJECTIVES)}")
    if int(max_evals) < 1:
        raise ValueError("max_evals must be >= 1")
    max_evals = int(max_evals)
    engine = engine or NecEngine()
    deadline = time.perf_counter() + max(1.0, float(timeout_s))

    normalised = _normalise(variables)
    model = parse_deck(deck)
    if not model.wires:
        raise ValueError("the deck contains no GW (wire) cards")

    values: dict[str, float] = {}
    for var in normalised:
        current = _initial_value(var, model)
        values[var.name] = _clamp(current, var.lo, var.hi) if var.hi > var.lo else var.lo

    cache: dict[tuple, tuple[float, float | None, dict, str]] = {}
    evaluations = 0
    history: list[dict] = []

    def evaluate(candidate: dict[str, float]) -> tuple[float, float | None, dict, str] | None:
        """Run one candidate; returns (fitness, natural, metrics, deck)."""
        nonlocal evaluations
        key = tuple(sorted((name, round(value, 9)) for name, value in candidate.items()))
        if key in cache:
            return cache[key]
        if evaluations >= max_evals or time.perf_counter() >= deadline:
            return None
        evaluations += 1
        try:
            rendered = apply_variables(deck, candidate, variables)
            result = engine.run(rendered, timeout_s=max(1.0, min(60.0, deadline - time.perf_counter())))
            parsed = parse_output(result.output)
            if not parsed.blocks:
                raise ValueError("engine produced no frequency block")
            metrics = summarize_block(parsed.blocks[0], float(z0))
        except Exception as exc:  # noqa: BLE001 - one bad candidate must not abort the search
            metrics = {"error": str(exc)}
            record = (_NEG_INF, None, metrics, deck)
            cache[key] = record
            return record
        natural = _natural_score(metrics, objective)
        record = (_fitness(natural, objective), natural, metrics, rendered)
        cache[key] = record
        return record

    first = evaluate(values)
    if first is None:  # pragma: no cover - max_evals >= 1 guarantees one run
        raise ValueError("could not evaluate the initial candidate")
    best_fitness, best_natural, best_metrics, best_deck = first
    best_values = dict(values)
    history.append(
        {
            "eval": evaluations,
            "accepted": True,
            "values": _round_values(values),
            "score": _json_number(best_natural) if best_natural is not None else None,
            "best": _round_values(best_values),
        }
    )

    steps = {var.name: var.step for var in normalised}
    min_steps = {var.name: max(var.step * 0.05, 1e-9) for var in normalised}
    active = [var for var in normalised if var.hi > var.lo]
    while active and evaluations < max_evals and time.perf_counter() < deadline:
        improved = False
        for var in active:
            if evaluations >= max_evals or time.perf_counter() >= deadline:
                break
            step = steps[var.name]
            if step <= 0:
                continue
            best_try: tuple[float, float | None, dict, str] | None = None
            best_try_value = values[var.name]
            for sign in (1.0, -1.0):
                candidate_value = _clamp(values[var.name] + sign * step, var.lo, var.hi)
                rounded = round(candidate_value, 9)
                if rounded == round(values[var.name], 9):
                    continue
                candidate = dict(values)
                candidate[var.name] = rounded
                record = evaluate(candidate)
                if record is None:
                    continue
                if best_try is None or record[0] > best_try[0]:
                    best_try, best_try_value = record, rounded
            if best_try is not None and best_try[0] > best_fitness + 1e-12:
                best_fitness, best_natural, best_metrics, best_deck = best_try
                values[var.name] = best_try_value
                best_values = dict(values)
                improved = True
                history.append(
                    {
                        "eval": evaluations,
                        "accepted": True,
                        "values": _round_values(values),
                        "score": _json_number(best_natural) if best_natural is not None else None,
                        "best": _round_values(best_values),
                    }
                )
            elif best_try is not None:
                history.append(
                    {
                        "eval": evaluations,
                        "accepted": False,
                        "values": _round_values({**values, var.name: best_try_value}),
                        "score": _json_number(best_try[1]) if best_try[1] is not None else None,
                        "best": _round_values(best_values),
                    }
                )
        if not improved:
            done = True
            for var in active:
                steps[var.name] = steps[var.name] * 0.5
                if steps[var.name] >= min_steps[var.name]:
                    done = False
            if done:
                break

    return {
        "objective": objective,
        "best": _round_values(best_values),
        "score": _json_number(best_natural) if best_natural is not None else None,
        "fitness": _json_number(best_fitness),
        "metrics": _sanitize_metrics(best_metrics),
        "evaluations": evaluations,
        "max_evals": max_evals,
        "history": history,
        "best_deck": best_deck,
        "variables": {
            var.name: {
                "kind": var.kind,
                "min": var.lo,
                "max": var.hi,
                "step": var.step,
                **({"index": var.index} if var.index else {}),
                **({"field": var.field} if var.field else {}),
            }
            for var in normalised
        },
    }


def _initial_value(var: _Var, model) -> float:
    """Best guess for the starting value of a variable inside its range."""
    if var.initial is not None:
        return var.initial
    mid = (var.lo + var.hi) / 2.0
    if var.hi <= var.lo:
        return var.lo
    try:
        if var.kind == "symbol":
            current = model.symbols.get(var.name.upper())
            if current is not None and var.lo <= current <= var.hi:
                return float(current)
        elif var.kind == "element_length":
            wires = _element_wires(model, var.index or 1)
            return _clamp(wires[0].length_m, var.lo, var.hi)
        elif var.kind == "element_position":
            wires = _element_wires(model, var.index or 1)
            wire = wires[0]
            axis = _position_axis(model, _length_axis(wire))
            centre = {"x": wire.center[0], "y": wire.center[1], "z": wire.center[2]}[axis]
            return _clamp(centre, var.lo, var.hi)
    except (ValueError, ModelError):
        return mid
    return mid


def _round_values(values: dict[str, float]) -> dict[str, float]:
    return {name: round(float(value), 6) for name, value in values.items()}


def _sanitize_metrics(metrics: dict) -> dict:
    """Make a metrics dict JSON-safe (non-finite numbers become None)."""
    clean: dict = {}
    for key, value in metrics.items():
        if isinstance(value, float) and not math.isfinite(value):
            clean[key] = None
        elif isinstance(value, dict):
            clean[key] = _sanitize_metrics(value)
        elif isinstance(value, list):
            clean[key] = [_sanitize_metrics(v) if isinstance(v, dict) else
                          (None if isinstance(v, float) and not math.isfinite(v) else v) for v in value]
        else:
            clean[key] = value
    return clean
