"""NEC-2 card-deck parsing, symbol evaluation and editing.

4NEC2 models frequently use ``SY`` symbols and arithmetic expressions inside
GW/FR cards (``GW 1 21 Xr -Hr Zel ...``).  This module evaluates those
expressions with a small safe AST evaluator so tools can describe and modify
models without guessing.
"""

from __future__ import annotations

import ast
import math
import re
from dataclasses import dataclass, field

_LABELS = {
    "CM", "CE", "CT", "CK", "GW", "GE", "GN", "EX", "FR", "RP", "LD", "TL",
    "SY", "EK", "EN", "NH", "GC", "GM", "SP", "SM", "SC", "GA", "IS", "PT", "XQ",
    "NX", "NE", "EK",
}


class ModelError(ValueError):
    pass


@dataclass
class Card:
    label: str
    fields: list[str]
    raw: str
    line_no: int


@dataclass
class Wire:
    tag: int
    segments: int
    x1: float
    y1: float
    z1: float
    x2: float
    y2: float
    z2: float
    radius: float
    line_no: int = 0

    @property
    def length_m(self) -> float:
        return math.dist((self.x1, self.y1, self.z1), (self.x2, self.y2, self.z2))

    @property
    def center(self) -> tuple[float, float, float]:
        return (
            (self.x1 + self.x2) / 2.0,
            (self.y1 + self.y2) / 2.0,
            (self.z1 + self.z2) / 2.0,
        )

    def orientation(self) -> str:
        deltas = {
            "X": abs(self.x2 - self.x1),
            "Y": abs(self.y2 - self.y1),
            "Z": abs(self.z2 - self.z1),
        }
        axis = max(deltas, key=deltas.get)
        return axis if deltas[axis] > 1e-9 else "point"


@dataclass
class Model:
    text: str
    cards: list[Card] = field(default_factory=list)
    symbols: dict[str, float] = field(default_factory=dict)
    wires: list[Wire] = field(default_factory=list)
    frequencies_mhz: list[float] = field(default_factory=list)
    ground: dict = field(default_factory=dict)
    excitations: list[dict] = field(default_factory=list)
    rp_cards: list[dict] = field(default_factory=list)
    comment: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def total_segments(self) -> int:
        return sum(w.segments for w in self.wires)

    def bounding_box(self) -> dict:
        if not self.wires:
            return {}
        xs = [c for w in self.wires for c in (w.x1, w.x2)]
        ys = [c for w in self.wires for c in (w.y1, w.y2)]
        zs = [c for w in self.wires for c in (w.z1, w.z2)]
        return {
            "dx_m": round(max(xs) - min(xs), 4),
            "dy_m": round(max(ys) - min(ys), 4),
            "dz_m": round(max(zs) - min(zs), 4),
            "x": [round(min(xs), 4), round(max(xs), 4)],
            "y": [round(min(ys), 4), round(max(ys), 4)],
            "z": [round(min(zs), 4), round(max(zs), 4)],
        }


_ALLOWED_CALLS = {
    "sin": lambda v: math.sin(math.radians(v)),
    "cos": lambda v: math.cos(math.radians(v)),
    "tan": lambda v: math.tan(math.radians(v)),
    "atn": math.atan,
    "atan": math.atan,
    "asin": lambda v: math.degrees(math.asin(v)),
    "acos": lambda v: math.degrees(math.acos(v)),
    "sqrt": math.sqrt,
    "sqr": lambda v: v * v,
    "abs": abs,
    "exp": math.exp,
    "log": math.log10,
    "ln": math.log,
}

_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Name, ast.Load,
    ast.Call, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod, ast.USub,
    ast.UAdd,
)

_SYMBOL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\b")


def eval_expr(expr: str, symbols: dict[str, float] | None = None) -> float:
    """Evaluate a NEC symbol expression (arithmetic + a few functions).

    Symbol lookup is case-insensitive: 4NEC2 decks routinely declare
    ``SY Lr=0.322`` and later reference ``Lr/2``.
    """
    symbols = {str(k).upper(): v for k, v in (symbols or {}).items()}
    text = expr.strip().replace("^", "**")
    if not text:
        raise ModelError("empty expression")
    if text.lower() in {"pi", "p i"}:
        return math.pi
    try:
        value = float(text)
        return value
    except ValueError:
        pass
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise ModelError(f"cannot parse expression {expr!r}: {exc}") from exc
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ModelError(f"unsupported token in expression {expr!r}")
        if (
            isinstance(node, ast.Name)
            and node.id.lower() not in _ALLOWED_CALLS
            and node.id.lower() != "pi"
            and node.id.upper() not in symbols
        ):
            raise ModelError(f"undefined symbol {node.id!r} in expression {expr!r}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id.lower() not in _ALLOWED_CALLS:
                raise ModelError(f"unsupported function in expression {expr!r}")
    # NEC symbol names are case-insensitive; upper-case the variable names so
    # the Python ``eval`` below can resolve them.
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id.lower() not in _ALLOWED_CALLS and node.id.lower() != "pi":
            node.id = node.id.upper()
    try:
        return float(eval(compile(tree, "<nec-expr>", "eval"), {"__builtins__": {}}, {  # noqa: S307
            **_ALLOWED_CALLS, **symbols, "pi": math.pi,
        }))
    except Exception as exc:  # pragma: no cover - defensive
        raise ModelError(f"cannot evaluate expression {expr!r}: {exc}") from exc


def _try_expr(token: str, symbols: dict[str, float]) -> float | None:
    try:
        return eval_expr(token, symbols)
    except ModelError:
        return None


def has_expressions(text: str) -> bool:
    """True when the deck uses SY symbols somewhere."""
    return any(line[:2].upper() == "SY" for line in text.splitlines())


def parse_deck(text: str) -> Model:
    """Parse a NEC-2 deck, evaluating SY symbols where possible."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    model = Model(text=text)
    comments: list[str] = []

    for line_no, raw in enumerate(text.split("\n"), start=1):
        line = raw.rstrip()
        if not line.strip():
            continue
        label = line[:2].upper()
        if label in ("CM", "CE", "CT", "CK"):
            comments.append(line[2:].strip())
            model.cards.append(Card(label=label, fields=[], raw=line, line_no=line_no))
            continue
        if label not in _LABELS:
            model.warnings.append(f"line {line_no}: unrecognised card label {label!r}")
            continue
        fields = line[2:].split()
        model.cards.append(Card(label=label, fields=fields, raw=line, line_no=line_no))

        if label == "SY":
            _parse_sy(fields, model, line_no)
        elif label == "GW":
            wire = _parse_gw(fields, model.symbols, line_no, model)
            if wire:
                model.wires.append(wire)
        elif label == "FR":
            _parse_fr(fields, model, line_no)
        elif label == "GN":
            _parse_gn(fields, model, line_no)
        elif label == "EX":
            _parse_ex(fields, model, line_no)
        elif label == "RP":
            _parse_rp(fields, model, line_no)
        elif label == "GE":
            pass
        elif label == "EN":
            break

    model.comment = " ".join(c for c in comments if c)
    if not model.wires:
        model.errors.append("deck contains no GW (wire) cards")
    if not model.excitations:
        model.errors.append("deck contains no EX (excitation) card")
    if not model.frequencies_mhz:
        model.errors.append("deck contains no FR (frequency) card")
    return model


def _parse_sy(fields: list[str], model: Model, line_no: int) -> None:
    if not fields:
        return
    name = fields[0]
    if "=" in name:
        name, expr = name.split("=", 1)
        expr = " ".join([expr] + fields[1:])
    elif len(fields) > 1:
        expr = " ".join(fields[1:])
    else:
        expr = f"{name}=0"
        name, expr = expr.split("=", 1)
    try:
        model.symbols[name.strip().upper()] = eval_expr(expr, model.symbols)
    except ModelError as exc:
        model.warnings.append(f"line {line_no}: cannot evaluate SY {name!r}: {exc}")


def _number(token: str, symbols: dict[str, float], line_no: int, model: Model, field_name: str) -> float | None:
    value = _try_expr(token, symbols)
    if value is None:
        model.warnings.append(f"line {line_no}: cannot parse {field_name} value {token!r}")
    return value


def _parse_gw(fields: list[str], symbols: dict[str, float], line_no: int, model: Model) -> Wire | None:
    if len(fields) < 9:
        model.warnings.append(f"line {line_no}: GW card needs 9 fields, got {len(fields)}")
        return None
    names = ["tag", "segments", "x1", "y1", "z1", "x2", "y2", "z2", "radius"]
    values: list[float] = []
    for name, token in zip(names, fields[:9]):
        value = _number(token, symbols, line_no, model, name)
        if value is None:
            return None
        values.append(value)
    return Wire(
        tag=int(round(values[0])),
        segments=max(1, int(round(values[1]))),
        x1=values[2], y1=values[3], z1=values[4],
        x2=values[5], y2=values[6], z2=values[7],
        radius=values[8], line_no=line_no,
    )


def _parse_fr(fields: list[str], model: Model, line_no: int) -> None:
    if len(fields) < 6:
        model.warnings.append(f"line {line_no}: FR card needs 6 fields")
        return
    try:
        n_freq = int(float(fields[1]))
        f_start = eval_expr(fields[4], model.symbols)
        f_step = eval_expr(fields[5], model.symbols)
    except (ModelError, ValueError) as exc:
        model.warnings.append(f"line {line_no}: cannot parse FR card: {exc}")
        return
    if n_freq <= 1:
        model.frequencies_mhz = [round(f_start, 4)]
    else:
        model.frequencies_mhz = [round(f_start + i * f_step, 4) for i in range(n_freq)]


_GROUND_NAMES = {-1: "free space", 0: "perfect ground", 1: "perfect ground", 2: "Sommerfeld / finite ground"}


def _parse_gn(fields: list[str], model: Model, line_no: int) -> None:
    try:
        code = int(float(fields[0]))
    except (IndexError, ValueError):
        model.warnings.append(f"line {line_no}: cannot parse GN card")
        return
    entry = {"code": code, "type": _GROUND_NAMES.get(code, f"ground type {code}")}
    if code == 2 and len(fields) >= 6:
        entry["relative_permittivity"] = _try_expr(fields[3], model.symbols)
        entry["conductivity_s_per_m"] = _try_expr(fields[4], model.symbols)
    model.ground = entry


def _parse_ex(fields: list[str], model: Model, line_no: int) -> None:
    if len(fields) < 4:
        return
    try:
        ex_type = int(float(fields[0]))
        tag = int(float(fields[1]))
        segment = fields[2]
        model.excitations.append(
            {
                "type": "voltage" if ex_type == 0 else ("current" if ex_type == 5 else f"type {ex_type}"),
                "tag": tag,
                "segment": segment,
                "voltage": _try_expr(fields[4], model.symbols) if len(fields) > 4 else None,
            }
        )
    except ValueError:
        model.warnings.append(f"line {line_no}: cannot parse EX card")


def _parse_rp(fields: list[str], model: Model, line_no: int) -> None:
    try:
        n_theta = int(float(fields[1]))
        n_phi = int(float(fields[2]))
        theta0 = _try_expr(fields[4], model.symbols)
        phi0 = _try_expr(fields[5], model.symbols)
        d_theta = _try_expr(fields[6], model.symbols) if len(fields) > 6 else 0.0
        d_phi = _try_expr(fields[7], model.symbols) if len(fields) > 7 else 0.0
    except (IndexError, ValueError):
        model.warnings.append(f"line {line_no}: cannot parse RP card")
        return
    model.rp_cards.append(
        {
            "n_theta": n_theta,
            "n_phi": n_phi,
            "theta_start_deg": theta0,
            "phi_start_deg": phi0,
            "theta_step_deg": d_theta,
            "phi_step_deg": d_phi,
        }
    )


def describe_model(model: Model) -> dict:
    """Build a JSON-friendly description of a parsed deck."""
    elements = []
    seen: dict[int, list[Wire]] = {}
    for wire in model.wires:
        seen.setdefault(wire.tag, []).append(wire)
    for tag, wires in sorted(seen.items()):
        total_len = sum(w.length_m for w in wires)
        centers = [w.center for w in wires]
        elements.append(
            {
                "tag": tag,
                "parts": len(wires),
                "segments": sum(w.segments for w in wires),
                "total_length_m": round(total_len, 4),
                "radius_m": round(wires[0].radius, 5),
                "orientation": wires[0].orientation(),
                "center_m": [round(c, 4) for c in centers[0]],
            }
        )
    return {
        "comment": model.comment[:500],
        "frequency_mhz": model.frequencies_mhz,
        "ground": model.ground,
        "excitation": model.excitations,
        "wires": [
            {
                "tag": w.tag,
                "segments": w.segments,
                "start_m": [w.x1, w.y1, w.z1],
                "end_m": [w.x2, w.y2, w.z2],
                "length_m": round(w.length_m, 4),
                "radius_m": round(w.radius, 5),
                "orientation": w.orientation(),
            }
            for w in model.wires
        ],
        "elements": elements,
        "total_segments": model.total_segments,
        "bounding_box": model.bounding_box(),
        "radiation_patterns": model.rp_cards,
        "symbols": {k: round(v, 6) for k, v in model.symbols.items()},
        "errors": model.errors,
        "warnings": model.warnings,
    }


def set_frequency(text: str, freqs_mhz: list[float]) -> str:
    """Replace the FR card with an explicit frequency list (as a multi-frequency FR)."""
    if not freqs_mhz:
        raise ModelError("no frequencies given")
    freqs = sorted(float(f) for f in freqs_mhz)
    if len(freqs) == 1:
        new_line = f"FR 0 0 0 0 {freqs[0]:.6g} 0"
    else:
        step = freqs[1] - freqs[0]
        uniform = all(abs((b - a) - step) < 1e-6 for a, b in zip(freqs, freqs[1:]))
        if uniform:
            new_line = f"FR 0 {len(freqs)} 0 0 {freqs[0]:.6g} {step:.6g}"
        else:
            # Non-uniform lists are handled by running one deck per frequency,
            # so this function only supports uniform stepping.
            raise ModelError("non-uniform frequency lists are not supported by set_frequency")
    return _replace_card(text, "FR", new_line)


def set_single_frequency(text: str, freq_mhz: float) -> str:
    return _replace_card(text, "FR", f"FR 0 0 0 0 {float(freq_mhz):.6g} 0")


def _replace_card(text: str, label: str, new_line: str) -> str:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    replaced = False
    out: list[str] = []
    for line in lines:
        if line[:2].upper() == label:
            if replaced:
                continue
            out.append(new_line)
            replaced = True
        elif line[:2].upper() == "EN" and not replaced:
            out.append(new_line)
            replaced = True
            out.append(line)
        else:
            out.append(line)
    if not replaced:
        out.insert(0 if not out else len(out) - 1, new_line)
    return "\n".join(out) + ("\n" if not text.endswith("\n") else "")


def with_single_mhz(text: str, freq_mhz: float) -> str:
    """Convenience alias used by the server when sweeping one frequency at a time."""
    return set_single_frequency(text, freq_mhz)
