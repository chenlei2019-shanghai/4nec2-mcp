"""Evaluate an external 3-band Yagi table with both feed topologies.

The table below is the geometry a ham supplied for a "parallel-fed" 10/15/20 m
tribander: one boom, reflectors at the back, all three driven elements soldered
in parallel to a single balun.  We rebuild it verbatim and compare

* ``separate``: one driven element excited at a time (switched feeds), and
* ``parallel``: all three driven elements wired to one balun (the real topology).

Feeding the *same* metal with two different feed networks is the whole point:
the SWR numbers are not interchangeable.

Usage::

    python scripts/design_parallel_reference.py --report
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "scripts"))

from design_triband import BANDS, Design, Element, wavelength  # noqa: E402
from nec2core.engine import NecEngine, find_nec4nec_dir  # noqa: E402
from nec2core.metrics import summarize_block  # noqa: E402
from nec2core.output import parse_output  # noqa: E402

# name, band, role, single-side length (m), boom position (m)
REFERENCE_TABLE = [
    ("20R", "20m", "reflector", 5.60, 0.0),
    ("10R", "10m", "reflector", 2.74, 0.7),
    ("15R", "15m", "reflector", 3.75, 1.4),
    ("10D", "10m", "driven", 2.60, 2.4),
    ("20D", "20m", "driven", 5.20, 2.9),
    ("15D", "15m", "driven", 3.50, 3.3),
    ("10Dir", "10m", "director", 2.43, 4.0),
]

SEGMENTS_PER_METRE = 4.0


def reference_design() -> Design:
    elements = []
    for name, band, role, side, z in REFERENCE_TABLE:
        length = side * 2.0
        elements.append(
            Element(
                name=name,
                band=band,
                role=role,
                length=length,
                z=z,
                segments=max(7, int(round(length * SEGMENTS_PER_METRE))),
            )
        )
    return Design(
        elements=elements,
        name="parallel_reference",
        comment="reference parallel-fed tribander (10/15/20 m) from a ham table",
    )


def evaluate(engine: NecEngine, design: Design, band: str, freq_mhz: float, feed: str) -> dict:
    deck = design.parallel_deck(freq_mhz=freq_mhz) if feed == "parallel" else design.deck(band, freq_mhz=freq_mhz)
    result = engine.run(deck)
    if not result.ok:
        return {"status": "error", "error": "; ".join(result.errors) or "engine failed"}
    blocks = parse_output(result.output).blocks
    if not blocks:
        return {"status": "error", "error": "no frequency block"}
    summary = summarize_block(blocks[0], 50.0)
    summary["status"] = "ok"
    return summary


def band_rows(engine: NecEngine, design: Design, band: str, feed: str, points: int = 9) -> list[dict]:
    info = BANDS[band]
    rows = []
    for index in range(points):
        freq = info["low"] + (info["high"] - info["low"]) * index / (points - 1)
        row = evaluate(engine, design, band, freq, feed)
        if row.get("status") == "ok":
            rows.append(row)
    return rows


def report(engine: NecEngine, design: Design) -> dict:
    payload: dict = {"design": design.describe(), "bands": {}}
    for band in BANDS:
        entry: dict = {}
        for feed in ("separate", "parallel"):
            rows = band_rows(engine, design, band, feed)
            if not rows:
                entry[feed] = {"status": "error"}
                continue
            center = min(rows, key=lambda row: abs(row["freq_mhz"] - BANDS[band]["center"]))
            entry[feed] = {
                "worst_swr": round(max(row["swr"] for row in rows), 3),
                "best_swr": round(min(row["swr"] for row in rows), 3),
                "center": {
                    "freq_mhz": center["freq_mhz"],
                    "swr": round(center["swr"], 3),
                    "z": [round(center["z_real"], 2), round(center["z_imag"], 2)],
                    "gain_max_db": round(center["gain_max_db"], 2),
                    "fb_db": round(center["fb_db"], 1) if center["fb_db"] is not None else None,
                    "efficiency_percent": round(center["efficiency_percent"], 2),
                },
            }
        payload["bands"][band] = entry
    return payload


def write_decks(design: Design, out_dir: Path) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    for band in BANDS:
        path = out_dir / f"{design.name}_{band}.nec"
        path.write_text(design.deck(band, sphere=True), encoding="ascii")
        paths[band] = str(path)
    parallel = out_dir / f"{design.name}_parallel.nec"
    parallel.write_text(design.parallel_deck(freq_mhz=BANDS["15m"]["center"], sphere=True), encoding="ascii")
    paths["parallel"] = str(parallel)
    return paths


def with_geometry(
    design: Design,
    lengths: dict[str, float] | None = None,
    positions: dict[str, float] | None = None,
) -> Design:
    lengths = lengths or {}
    positions = positions or {}
    elements = [
        replace(
            element,
            length=lengths.get(element.name, element.length),
            z=positions.get(element.name, element.z),
        )
        for element in design.elements
    ]
    return Design(
        elements=elements,
        name=design.name,
        comment=design.comment,
        radius=design.radius,
        conductivity=design.conductivity,
        ground=design.ground,
        meta=design.meta,
    )


def with_lengths(design: Design, lengths: dict[str, float]) -> Design:
    return with_geometry(design, lengths=lengths)


def tune_positions(
    engine: NecEngine,
    design: Design,
    *,
    names: tuple[str, ...] = ("10R", "15R", "10D", "20D", "15D", "10Dir"),
    steps: tuple[float, ...] = (0.10, 0.05, 0.02),
    points: int = 3,
    min_gap: float = 0.15,
    verbose: bool = True,
) -> tuple[Design, dict]:
    best = feed_metrics(engine, design, points=points)
    current = design
    if verbose:
        print(f"[tune-z] start worst SWR {best['score']:.3f}", flush=True)
    for step in steps:
        improved = True
        while improved:
            improved = False
            for name in names:
                base = next(element.z for element in current.elements if element.name == name)
                for delta in (-step, step):
                    candidate_z = round(base + delta, 4)
                    others = sorted(
                        element.z for element in current.elements if element.name != name
                    )
                    if any(abs(candidate_z - other) < min_gap for other in others):
                        continue
                    if not (0.0 < candidate_z <= 5.0):
                        continue
                    candidate = with_geometry(current, positions={name: candidate_z})
                    metrics = feed_metrics(engine, candidate, points=points)
                    if metrics["score"] < best["score"] - 1e-3:
                        best = metrics
                        current = candidate
                        improved = True
                        if verbose:
                            print(
                                f"  {name} z -> {candidate_z:.3f} m  worst SWR {best['score']:.3f}  "
                                + " ".join(f"{b}:{m['worst_swr']:.2f}" for b, m in best["bands"].items()),
                                flush=True,
                            )
                        break
    return current, best


def feed_metrics(engine: NecEngine, design: Design, points: int = 3) -> dict:
    """Worst in-band SWR per band with every driven element paralleled to one balun."""
    bands: dict[str, dict] = {}
    for band in BANDS:
        info = BANDS[band]
        rows = []
        for index in range(points):
            freq = info["low"] + (info["high"] - info["low"]) * index / (points - 1)
            row = evaluate(engine, design, band, freq, "parallel")
            if row.get("status") == "ok":
                rows.append(row)
        if not rows:
            return {"score": 1e9, "bands": {}}
        center = min(rows, key=lambda row: abs(row["freq_mhz"] - info["center"]))
        bands[band] = {
            "worst_swr": max(row["swr"] for row in rows),
            "center_swr": center["swr"],
            "center_gain_db": center["gain_max_db"],
        }
    worst = max(entry["worst_swr"] for entry in bands.values())
    gain = sum(entry["center_gain_db"] for entry in bands.values())
    return {"score": worst, "bands": bands, "gain_sum": gain}


def tune_parallel(
    engine: NecEngine,
    design: Design,
    *,
    names: tuple[str, ...] = ("20R", "10R", "15R", "10D", "20D", "15D", "10Dir"),
    steps: tuple[float, ...] = (0.08, 0.04, 0.02, 0.01, 0.005),
    points: int = 3,
    verbose: bool = True,
) -> tuple[Design, dict]:
    best = feed_metrics(engine, design, points=points)
    current = design
    if verbose:
        print(f"[tune] start worst SWR {best['score']:.3f}", flush=True)
    for step in steps:
        improved = True
        while improved:
            improved = False
            for name in names:
                base = next(element.length for element in current.elements if element.name == name)
                for factor in (1.0 - step, 1.0 + step):
                    candidate = with_lengths(
                        current, {name: round(base * factor, 5)}
                    )
                    metrics = feed_metrics(engine, candidate, points=points)
                    if metrics["score"] < best["score"] - 1e-3:
                        best = metrics
                        current = candidate
                        improved = True
                        if verbose:
                            print(
                                f"  {name} -> {base * factor:.4f} m  worst SWR {best['score']:.3f}  "
                                + " ".join(f"{b}:{m['worst_swr']:.2f}" for b, m in best["bands"].items()),
                                flush=True,
                            )
                        break
    return current, best


def draw_geometry(design: Design, out_path: Path) -> dict:
    """Render the plan-view geometry sketch (needs matplotlib)."""
    from nec2core import plots

    elements = [
        {
            "name": element.name,
            "band": element.band,
            "role": element.role,
            "length": element.length,
            "z": element.z,
            "segments": element.segments,
            "wavelength_m": wavelength(BANDS[element.band]["center"]),
        }
        for element in design.sorted_elements()
    ]
    return plots.geometry_schematic(
        elements,
        out_path,
        title="并联馈电三波段八木（10 / 15 / 20 m）· 几何示意",
        subtitle=(
            f"boom {design.boom_length:.3f} m；三个驱动单元的中心经母线并联到同一个巴伦"
            f"（单点 50 Ω 馈电）；黄色行为该波段的驱动单元"
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="rebuild an external parallel-fed tribander and evaluate it")
    parser.add_argument("--nec2-dir", default=None)
    parser.add_argument("--out", default=str(PROJECT / "designs" / "parallel_reference"))
    parser.add_argument("--tune", action="store_true", help="retune element lengths for the parallel feed")
    parser.add_argument(
        "--steps",
        default="0.08,0.04,0.02,0.01,0.005",
        help="comma separated relative steps for the coordinate search",
    )
    parser.add_argument("--points", type=int, default=3, help="in-band sample points per band while tuning")
    parser.add_argument(
        "--tune-positions",
        action="store_true",
        help="after tuning lengths, also slide elements along the boom",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="start from the tuned lengths stored in designs/parallel_reference/reference.json",
    )
    args = parser.parse_args(argv)

    engine = NecEngine(find_nec4nec_dir(args.nec2_dir))
    design = reference_design()
    out_dir = Path(args.out)
    stored = out_dir / "reference.json"
    if args.resume and stored.exists():
        previous = json.loads(stored.read_text(encoding="utf-8"))
        lengths = previous.get("tuned_lengths") or {}
        positions = previous.get("tuned_positions") or {}
        if lengths or positions:
            design = with_geometry(
                design,
                lengths={name: float(value) for name, value in lengths.items()},
                positions={name: float(value) for name, value in positions.items()},
            )
            print(
                f"[resume] starting from {len(lengths)} tuned lengths, {len(positions)} tuned positions",
                flush=True,
            )
    if args.tune:
        steps = tuple(float(value) for value in args.steps.split(",") if value.strip())
        design, metrics = tune_parallel(engine, design, steps=steps, points=args.points)
        if args.tune_positions:
            design, metrics = tune_positions(engine, design, points=args.points)
        design = replace(design, name="parallel_reference_tuned")
        print("[tune] final:", json.dumps(
            {band: {k: round(v, 3) for k, v in entry.items()} for band, entry in metrics["bands"].items()},
            ensure_ascii=False,
        ), flush=True)
    payload = report(engine, design)
    if args.tune:
        payload["tuned_lengths"] = {element.name: round(element.length, 4) for element in design.elements}
        payload["tuned_positions"] = {element.name: round(element.z, 4) for element in design.elements}
    payload["paths"] = write_decks(design, out_dir)
    try:
        payload["geometry"] = draw_geometry(design, out_dir / "geometry.png")
    except Exception as exc:  # matplotlib is optional
        print(f"[geometry] skipped: {type(exc).__name__}: {exc}", flush=True)
    payload["table"] = [
        {
            "name": name,
            "band": band,
            "role": role,
            "length_m": round(side * 2.0, 3),
            "side_m": side,
            "z_m": z,
            "length_lambda": round(side * 2.0 / wavelength(BANDS[band]["center"]), 3),
        }
        for name, band, role, side, z in REFERENCE_TABLE
    ]
    (out_dir / "reference.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    for band, entry in payload["bands"].items():
        separate = entry["separate"]
        parallel = entry["parallel"]
        center_s = separate["center"]
        center_p = parallel["center"]
        print(
            f"{band}: separate SWR {separate['best_swr']:.2f}-{separate['worst_swr']:.2f} "
            f"(center {center_s['swr']:.3f}, {center_s['gain_max_db']:.2f} dBi) | "
            f"parallel SWR {parallel['best_swr']:.2f}-{parallel['worst_swr']:.2f} "
            f"(center {center_p['swr']:.3f}, {center_p['gain_max_db']:.2f} dBi, "
            f"Z {center_p['z'][0]:.1f}{center_p['z'][1]:+.1f}j)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
