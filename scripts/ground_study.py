"""Elevation patterns of the 10/15/20 m triband Yagi over real ground.

For each band the antenna is rotated into the "horizontal" frame (boom along
+X, elements along Y, every wire at height ``z = H``) so that NEC's ground
plane (z = 0) is the real earth, then the elevation cut in the boom plane
(phi = 0) is computed for

* free space (``GN -1``),
* average real ground (``GN 2``, eps_r = 13, sigma = 0.005 S/m) at 5 m, 8 m
  and 10 m above the ground plane.

Ground losses are folded into the radiated power by NEC-2's reflection
coefficient ground, so the power budget still reports 100 % "efficiency";
the ground effect shows up as a changed take-off angle and peak gain.

Usage::

    python scripts/ground_study.py                 # all bands, table + PNGs
    python scripts/ground_study.py --band 10m      # single band
    python scripts/ground_study.py --nec2-dir C:\\4nec2
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from nec2core import NecEngine, parse_output  # noqa: E402
from nec2core import plots  # noqa: E402

import design_triband as dt  # noqa: E402

#: condition key -> antenna centre height above ground (None = free space)
CONDITIONS: dict[str, float | None] = {
    "free": None,
    "5m": 5.0,
    "8m": 8.0,
    "10m": 10.0,
}

LABELS = {
    "free": "自由空间（无地面）",
    "5m": "真实地面 5 m",
    "8m": "真实地面 8 m",
    "10m": "真实地面 10 m",
}

COLORS = {
    "free": "#616161",
    "5m": "#c62828",
    "8m": "#1565c0",
    "10m": "#2e7d32",
}

#: average ground: relative permittivity 13, conductivity 0.005 S/m
GROUND_CARD = "GN 2 0 0 0 13.0 0.005"


def ground_deck(design: dt.Design, band: str, height_m: float | None) -> str:
    """Return a deck with the boom along +X at ``height_m`` above ground plane."""
    height = 0.0 if height_m is None else float(height_m)
    lines = [f"CM {design.comment}"]
    for element in design.sorted_elements():
        lines.append(
            f"CM {element.name:>6s} {element.band:>4s} {element.role:<9s} "
            f"L={element.length:7.4f}m boom_x={element.z:6.3f}m h={height:5.2f}m"
        )
    lines.append("CE")

    for index, element in enumerate(design.sorted_elements(), start=1):
        half = element.length / 2.0
        lines.append(
            f"GW {index} {element.segments} {element.z:.5f} {-half:.5f} {height:.5f} "
            f"{element.z:.5f} {half:.5f} {height:.5f} {design.radius:.4f}"
        )
    lines.append("GE 0")
    lines.append("EK")
    lines.append("GN -1" if height_m is None else GROUND_CARD)
    lines.append(f"LD 5 0 0 0 {design.conductivity:.4E}")

    driven = next(element for element in design.by_band(band) if element.driven)
    tag = design.sorted_elements().index(driven) + 1
    centre_segment = (driven.segments + 1) // 2
    lines.append(f"EX 0 {tag} {centre_segment} 0 1 0 0")
    lines.append(f"FR 0 0 0 0 {dt.BANDS[band]['center']:.4f} 0")
    # 1° theta steps in the phi = 0 plane (the plane that contains the boom)
    lines.append("RP 0 181 1 1000 0 0 1")
    lines.append("EN")
    return "\n".join(lines) + "\n"


def elevation_cut(output_text: str, phi_deg: float = 0.0) -> list[tuple[float, float]]:
    """Return ``[(elevation_deg, gain_db), ...]`` for the cut at ``phi_deg``."""
    parsed = parse_output(output_text)
    block = parsed.primary
    if block is None or not block.patterns:
        raise RuntimeError("engine produced no radiation pattern block")
    pairs: list[tuple[float, float]] = []
    for sample in block.patterns:
        if abs((sample.phi_deg or 0.0) - phi_deg) > 0.5:
            continue
        gain = sample.total_db
        if gain is None:
            continue
        pairs.append((90.0 - sample.theta_deg, float(gain)))
    if not pairs:
        raise RuntimeError(f"no samples at phi={phi_deg}")
    return sorted(pairs)


def gain_at(pairs: list[tuple[float, float]], elevation_deg: float, tol: float = 0.75) -> float | None:
    """Interpolate the gain at a given elevation (nearest sample)."""
    nearest = min(pairs, key=lambda pair: abs(pair[0] - elevation_deg))
    return nearest[1] if abs(nearest[0] - elevation_deg) <= tol else None


def study_band(engine: NecEngine, design: dt.Design, band: str, out_root: Path) -> dict:
    curves: list[dict] = []
    results: dict[str, dict] = {}
    for key, height in CONDITIONS.items():
        deck = ground_deck(design, band, height)
        run = engine.run(deck)
        if not run.ok:
            raise RuntimeError(f"{band}/{key} 引擎失败：{'；'.join(run.errors) or run.exit_code}")
        pairs = elevation_cut(run.output)
        curves.append({"label": LABELS[key], "color": COLORS[key], "pairs": pairs})
        above_horizon = [(angle, value) for angle, value in pairs if 0.0 <= angle <= 90.0]
        peak_angle, peak_gain = max(above_horizon, key=lambda pair: pair[1])
        results[key] = {
            "label": LABELS[key],
            "height_m": height,
            "gain_peak_db": round(peak_gain, 2),
            "takeoff_deg": round(peak_angle, 1),
            "gain_at_10deg_db": None if gain_at(pairs, 10.0) is None else round(gain_at(pairs, 10.0), 2),
            "gain_at_20deg_db": None if gain_at(pairs, 20.0) is None else round(gain_at(pairs, 20.0), 2),
            "duration_s": round(run.duration_s, 3),
        }

    out_dir = out_root / band
    out_dir.mkdir(parents=True, exist_ok=True)
    png = out_dir / f"{design.name}_{band}_elevation_vs_height.png"
    payload = plots.elevation_comparison_polar(
        curves,
        png,
        title=f"{band} 仰角方向图对比（{dt.BANDS[band]['center']:.2f} MHz）",
    )
    return {
        "band": band,
        "freq_mhz": dt.BANDS[band]["center"],
        "figure": payload["path"],
        "figure_bytes": png.stat().st_size,
        "conditions": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="三波段八木：自由空间 vs 真实地面方向图")
    parser.add_argument("--nec2-dir", default=r"C:\4nec2", help="4NEC2 installation directory")
    parser.add_argument("--band", action="append", choices=sorted(dt.BANDS), help="band(s) to study (default: all)")
    parser.add_argument("--out", default=str(ROOT / "plots" / "triband"), help="plot output root")
    parser.add_argument("--json", default=str(ROOT / "designs" / "triband_10_15_20" / "ground_patterns.json"))
    args = parser.parse_args(argv)

    engine = NecEngine(args.nec2_dir)
    design = dt.initial_design()
    bands = args.band or sorted(dt.BANDS)
    out_root = Path(args.out)

    report: dict[str, dict] = {}
    for band in bands:
        info = study_band(engine, design, band, out_root)
        report[band] = info
        print(f"[{band}] {dt.BANDS[band]['center']:.2f} MHz -> {info['figure']} ({info['figure_bytes']} B)")
        for key, row in info["conditions"].items():
            print(
                f"    {row['label']:<12s} 峰值 {row['gain_peak_db']:6.2f} dBi @ {row['takeoff_deg']:5.1f}°"
                f"   10°仰角 {row['gain_at_10deg_db']}   20°仰角 {row['gain_at_20deg_db']}"
            )

    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"json -> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
