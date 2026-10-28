"""Parametric design of a 3-band (10 m / 15 m / 20 m) nested Yagi.

Layout: one boom along +Z, elements along Y, all elements directly fed at
their centre (one feed point per band, the other bands' driven elements are
left open when a band is simulated).

Bands:
    10 m  28.0-29.7 MHz  3 elements (reflector, driven, director)
    15 m  21.0-21.45 MHz 2 elements (reflector, driven)
    20 m  14.0-14.35 MHz 2 elements (reflector, driven)

The script builds the deck, evaluates it with the local NEC-2 engine, and can
search element lengths for a low SWR in each band.  The final decks and a JSON
summary are written to ``designs/triband_10_15_20``.

Usage::

    python scripts/design_triband.py --optimize
    python scripts/design_triband.py --report
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from nec2core.engine import NecEngine, find_nec4nec_dir  # noqa: E402
from nec2core.metrics import summarize_block  # noqa: E402
from nec2core.output import parse_output  # noqa: E402

LIGHT_SPEED = 300.0  # MHz * m

BANDS = {
    "10m": {"low": 28.0, "high": 29.7, "center": 28.5, "elements": 3},
    "15m": {"low": 21.0, "high": 21.45, "center": 21.2, "elements": 2},
    "20m": {"low": 14.0, "high": 14.35, "center": 14.15, "elements": 2},
}

RADIUS = 0.010  # 20 mm aluminium tubing
CONDUCTIVITY = 3.54e7  # S/m, aluminium


def wavelength(freq_mhz: float) -> float:
    return LIGHT_SPEED / freq_mhz


@dataclass
class Element:
    name: str
    band: str
    role: str  # reflector | driven | director
    length: float  # metres, tip to tip
    z: float  # position along the boom, metres
    segments: int

    @property
    def driven(self) -> bool:
        return self.role == "driven"


@dataclass
class Design:
    elements: list[Element]
    name: str = "triband_10_15_20"
    comment: str = "3-band nested Yagi: 10m 3el / 15m 2el / 20m 2el, direct feed"
    radius: float = RADIUS
    conductivity: float = CONDUCTIVITY
    ground: str = "free"
    meta: dict = field(default_factory=dict)

    # -- geometry helpers -------------------------------------------------
    def sorted_elements(self) -> list[Element]:
        return sorted(self.elements, key=lambda element: element.z)

    def by_band(self, band: str) -> list[Element]:
        return [element for element in self.sorted_elements() if element.band == band]

    @property
    def boom_length(self) -> float:
        return max(element.z for element in self.elements)

    # -- deck -------------------------------------------------------------
    def deck(
        self,
        band: str,
        *,
        freq_mhz: float | None = None,
        sphere: bool = False,
        feed_scale: float = 1.0,
        extra_cards: list[str] | None = None,
    ) -> str:
        """Return a NEC deck for ``band`` with only that band's feed active."""
        lines = [f"CM {self.comment}"]
        for element in self.sorted_elements():
            lines.append(
                f"CM {element.name:>6s} {element.band:>4s} {element.role:<9s} "
                f"L={element.length:7.4f}m z={element.z:6.3f}m"
            )
        lines.append("CE")

        for index, element in enumerate(self.sorted_elements(), start=1):
            half = element.length / 2.0
            lines.append(
                f"GW {index} {element.segments} 0 {-half:.5f} {element.z:.5f} "
                f"0 {half:.5f} {element.z:.5f} {self.radius:.4f}"
            )
        lines.append("GE 0")
        lines.append("EK")
        lines.append(f"GN {-1 if self.ground == 'free' else 1}")
        lines.append(f"LD 5 0 0 0 {self.conductivity:.4E}")

        band_elements = self.by_band(band)
        driven = next(element for element in band_elements if element.driven)
        tag = self.sorted_elements().index(driven) + 1
        centre_segment = (driven.segments + 1) // 2
        lines.append(f"EX 0 {tag} {centre_segment} 0 1 0 0")

        if freq_mhz is None:
            band_info = BANDS[band]
            lines.append(f"FR 0 0 0 0 {band_info['center']:.4f} 0")
        else:
            lines.append(f"FR 0 0 0 0 {freq_mhz:.4f} 0")

        if sphere:
            lines.append("RP 0 37 73 1000 0 0 5 5")
        else:
            lines.append("RP 0 19 37 1000 0 0 10 10")

        if extra_cards:
            lines.extend(extra_cards)
        lines.append("EN")
        return "\n".join(lines) + "\n"

    # -- parallel feed ----------------------------------------------------
    def parallel_deck(
        self,
        *,
        freq_mhz: float | None = None,
        sphere: bool = False,
        bus_x: float = 0.060,
        bus_y: float = 0.050,
        gap_m: float = 0.020,
        feed_z: float | None = None,
        extra_cards: list[str] | None = None,
    ) -> str:
        """Return one deck with **all driven elements wired in parallel** to a balun.

        Real "direct feed" multi-band Yagis solder every driven element across
        the same balanced feed line, which then goes through one 1:1 balun to
        the coax.  Electrically that is one source, not three: the band being
        used still has to work with the other bands' driven elements connected
        in parallel across the feed point (they load it out of band).

        Geometry used here (documented so the numbers can be reproduced):

        * every driven element is split at its centre with a ``gap_m`` gap;
        * two bus wires run along the boom at ``x = bus_x``, ``y = ±bus_y``,
          joining the arms of all driven elements (6 short link wires);
        * a bridge segment across the two buses at ``feed_z`` carries the
          single ``EX`` source (that bridge is where the balun/coax sits).

        This deck is band independent: the same structure is swept over the
        10 m, 15 m and 20 m ranges.
        """
        lines = [f"CM {self.comment} (parallel-fed drivers, single balun)"]
        for element in self.sorted_elements():
            lines.append(
                f"CM {element.name:>6s} {element.band:>4s} {element.role:<9s} "
                f"L={element.length:7.4f}m z={element.z:6.3f}m"
            )
        lines.append("CE")

        driven = [element for element in self.sorted_elements() if element.driven]
        if not driven:
            raise ValueError("design has no driven elements")
        if feed_z is None:
            feed_z = driven[len(driven) // 2].z
        driven_zs = sorted(element.z for element in driven)

        tag = 0
        cards: list[str] = []
        junction_tags: dict[tuple[float, float], int] = {}

        def add_wire(segments: int, start: tuple[float, float, float],
                     end: tuple[float, float, float]) -> int:
            nonlocal tag
            tag += 1
            cards.append(
                f"GW {tag} {max(3, int(segments))} "
                f"{start[0]:.5f} {start[1]:.5f} {start[2]:.5f} "
                f"{end[0]:.5f} {end[1]:.5f} {end[2]:.5f} {self.radius:.4f}"
            )
            return tag

        # elements: plain wires, drivers split into two arms with a centre gap
        for element in self.sorted_elements():
            half = element.length / 2.0
            if element.driven:
                arm = max(3, round(element.segments * (half - gap_m / 2.0) / element.length))
                add_wire(arm, (0.0, -half, element.z), (0.0, -gap_m / 2.0, element.z))
                add_wire(arm, (0.0, gap_m / 2.0, element.z), (0.0, half, element.z))
            else:
                add_wire(element.segments, (0.0, -half, element.z), (0.0, half, element.z))

        # two bus wires running along the boom, split at every driven element
        for sign, bus_label in ((1.0, "upper"), (-1.0, "lower")):
            for start_z, stop_z in zip(driven_zs, driven_zs[1:]):
                add_wire(17 if (stop_z - start_z) > 0.5 else 5,
                         (bus_x, sign * bus_y, start_z), (bus_x, sign * bus_y, stop_z))
            del bus_label

        # link each arm's inner end to its bus wire
        for element in driven:
            for sign in (1.0, -1.0):
                add_wire(1, (0.0, sign * gap_m / 2.0, element.z),
                         (bus_x, sign * bus_y, element.z))

        # source bridge: this is the balun/coax point, one EX card below
        bridge_tag = add_wire(1, (bus_x, bus_y, feed_z), (bus_x, -bus_y, feed_z))

        lines.extend(cards)
        lines.append("GE 0")
        lines.append("EK")
        lines.append(f"GN {-1 if self.ground == 'free' else 1}")
        lines.append(f"LD 5 0 0 0 {self.conductivity:.4E}")
        lines.append(f"EX 0 {bridge_tag} 1 0 1 0 0")

        if freq_mhz is None:
            lines.append(f"FR 0 0 0 0 {BANDS['10m']['center']:.4f} 0")
        else:
            lines.append(f"FR 0 0 0 0 {freq_mhz:.4f} 0")

        if sphere:
            lines.append("RP 0 37 73 1000 0 0 5 5")
        else:
            lines.append("RP 0 19 37 1000 0 0 10 10")

        if extra_cards:
            lines.extend(extra_cards)
        lines.append("EN")
        return "\n".join(lines) + "\n"

    # -- descriptions -----------------------------------------------------
    def describe(self) -> dict:
        return {
            "name": self.name,
            "boom_length_m": round(self.boom_length, 4),
            "radius_m": self.radius,
            "bands": {
                band: {
                    "freq_center_mhz": BANDS[band]["center"],
                    "elements": [
                        {
                            "name": element.name,
                            "role": element.role,
                            "length_m": round(element.length, 4),
                            "z_m": round(element.z, 4),
                            "segments": element.segments,
                        }
                        for element in self.by_band(band)
                    ],
                }
                for band in BANDS
            },
        }

    def replace_element(self, name: str, **changes) -> "Design":
        elements = [
            replace(element, **changes) if element.name == name else element
            for element in self.elements
        ]
        return replace(self, elements=elements)


# ---------------------------------------------------------------------------
# initial geometry
# ---------------------------------------------------------------------------
def initial_design() -> Design:
    """Final tuned geometry (lengths in metres, positions along +Z in metres).

    The layout is 20m at the back of the boom, 15m in the middle and 10m at the
    front; every band has its own driven element so each can be fed directly
    (50 ohm coax at the centre) while the other bands' drivers are left open.
    """
    elements = [
        Element("20R", "20m", "reflector", 10.6171, 0.00, 41),
        Element("15R", "15m", "reflector", 7.4490, 1.10, 31),
        Element("10R", "10m", "reflector", 5.2630, 2.60, 21),
        Element("20D", "20m", "driven", 9.8789, 3.30, 41),
        Element("15D", "15m", "driven", 6.6603, 4.10, 31),
        Element("10D", "10m", "driven", 4.8630, 5.60, 21),
        Element("10Dir", "10m", "director", 4.3157, 7.80, 19),
    ]
    return Design(elements=elements)


# ---------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------
def evaluate(
    engine: NecEngine,
    design: Design,
    band: str,
    freq_mhz: float | None = None,
    *,
    feed: str = "separate",
) -> dict:
    if feed == "parallel":
        deck = design.parallel_deck(freq_mhz=freq_mhz)
    else:
        deck = design.deck(band, freq_mhz=freq_mhz)
    result = engine.run(deck)
    if not result.ok:
        return {"status": "error", "error": "; ".join(result.errors) or "engine failed",
                "raw": result.output[-400:]}
    output = parse_output(result.output)
    if not output.blocks:
        return {"status": "error", "error": "no frequency block in output"}
    block = output.primary
    summary = summarize_block(block)
    summary["status"] = "ok"
    summary["duration_s"] = round(result.duration_s, 3)
    return summary


def band_sweep(
    engine: NecEngine,
    design: Design,
    band: str,
    points: int = 11,
    *,
    feed: str = "separate",
) -> list[dict]:
    info = BANDS[band]
    freqs = [info["low"] + (info["high"] - info["low"]) * i / (points - 1) for i in range(points)]
    rows = []
    for freq in freqs:
        row = evaluate(engine, design, band, freq_mhz=freq, feed=feed)
        rows.append(row)
    return rows


def best_swr_point(engine: NecEngine, design: Design, band: str, *, feed: str = "separate") -> tuple[float, float]:
    """Frequency of the SWR minimum inside the band and its value."""
    rows = band_sweep(engine, design, band, points=9, feed=feed)
    ok = [row for row in rows if row.get("status") == "ok"]
    if not ok:
        return (float("nan"), float("nan"))
    best = min(ok, key=lambda row: row["swr"])
    return (best["freq_mhz"], best["swr"])


def score(engine: NecEngine, design: Design, band: str, *, feed: str = "separate") -> dict:
    rows = band_sweep(engine, design, band, points=9, feed=feed)
    ok = [row for row in rows if row.get("status") == "ok"]
    if len(ok) != len(rows):
        return {"score": 1e9, "reason": "engine error"}
    worst_swr = max(row["swr"] for row in ok)
    center = min(ok, key=lambda row: abs(row["freq_mhz"] - BANDS[band]["center"]))
    gains = [row["gain_max_db"] for row in ok]
    fbs = [row["fb_db"] for row in ok if row["fb_db"] is not None]
    return {
        "score": round(worst_swr, 3),
        "in_band_worst_swr": round(worst_swr, 3),
        "center_swr": round(center["swr"], 3),
        "center_gain_db": round(center["gain_max_db"], 2),
        "min_gain_db": round(min(gains), 2),
        "min_fb_db": round(min(fbs), 1) if fbs else None,
        "efficiency_percent": round(center["efficiency_percent"], 2),
    }


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------
def tune_lengths(
    engine: NecEngine,
    design: Design,
    band: str,
    *,
    spans: int = 12,
    span_percent: float = 0.06,
    passes: int = 3,
    verbose: bool = True,
) -> Design:
    """Coordinate search on driven and reflector lengths to reduce worst in-band SWR."""
    band_elements = design.by_band(band)
    driven = next(element for element in band_elements if element.driven)
    reflector = next(element for element in band_elements if element.role == "reflector")

    for step, name in [(span_percent, driven.name), (span_percent, reflector.name)]:
        base = next(element.length for element in design.elements if element.name == name)
        best = None
        for index in range(spans + 1):
            factor = 1.0 - step + 2.0 * step * index / spans
            candidate = design.replace_element(name, length=round(base * factor, 5))
            result = score(engine, candidate, band)
            if verbose:
                print(f"  {band} {name} x{factor:.3f} -> L={base * factor:.4f} "
                      f"swr_max={result['score']}", flush=True)
            if best is None or result["score"] < best[0]:
                best = (result["score"], candidate)
        design = best[1]

    for _ in range(passes - 1):
        for name in (driven.name, reflector.name):
            base = next(element.length for element in design.elements if element.name == name)
            step = 0.02
            best = None
            for index in range(9):
                factor = 1.0 - step + 2.0 * step * index / 8
                candidate = design.replace_element(name, length=round(base * factor, 5))
                result = score(engine, candidate, band)
                if best is None or result["score"] < best[0]:
                    best = (result["score"], candidate)
            design = best[1]
    return design


# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------
def write_decks(design: Design, out_dir: Path) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}

    master = out_dir / f"{design.name}.nec"
    master.write_text(design.deck("10m", sphere=True), encoding="ascii")
    paths["master"] = str(master)

    for band in BANDS:
        target = out_dir / f"{design.name}_{band}.nec"
        target.write_text(design.deck(band, sphere=True), encoding="ascii")
        paths[band] = str(target)

    parallel = out_dir / f"{design.name}_parallel.nec"
    parallel.write_text(design.parallel_deck(freq_mhz=BANDS["15m"]["center"], sphere=True), encoding="ascii")
    paths["parallel"] = str(parallel)
    return paths


def draw_geometry(design: Design, out_path: Path) -> dict:
    """Render the plan-view geometry sketch and element table (needs matplotlib)."""
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
        title="三波段（10 / 15 / 20 m）单 boom 嵌套八木 · 几何示意",
        subtitle=(f"boom {design.boom_length:.3f} m；单元长度按真实尺寸绘制，"
                  f"下方数字为相邻单元的间距（m）；黄色行为该波段的馈电单元"),
    )


def band_report(engine: NecEngine, design: Design, *, feed: str = "separate") -> dict:
    report: dict = {"design": design.describe(), "feed": feed, "bands": {}}
    for band in BANDS:
        sweep = band_sweep(engine, design, band, points=11, feed=feed)
        rows = [row for row in sweep if row.get("status") == "ok"]
        if not rows:
            report["bands"][band] = {"status": "error"}
            continue
        center = min(rows, key=lambda row: abs(row["freq_mhz"] - BANDS[band]["center"]))
        low_edge = rows[0]
        high_edge = rows[-1]
        report["bands"][band] = {
            "center": {
                "freq_mhz": center["freq_mhz"],
                "swr": center["swr"],
                "z": [center["z_real"], center["z_imag"]],
                "gain_max_db": center["gain_max_db"],
                "gain_theta_phi": [center["gain_max_theta_deg"], center["gain_max_phi_deg"]],
                "fb_db": center["fb_db"],
                "efficiency_percent": center["efficiency_percent"],
            },
            "edges": {
                "low": [low_edge["freq_mhz"], round(low_edge["swr"], 3)],
                "high": [high_edge["freq_mhz"], round(high_edge["swr"], 3)],
            },
            "sweep": [
                {
                    "freq_mhz": row["freq_mhz"],
                    "swr": round(row["swr"], 3),
                    "gain_db": row["gain_max_db"],
                    "fb_db": row["fb_db"],
                    "efficiency_percent": row["efficiency_percent"],
                }
                for row in rows
            ],
        }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="3-band nested Yagi designer")
    parser.add_argument("--nec2-dir", default=None)
    parser.add_argument("--out", default=str(PROJECT / "designs" / "triband_10_15_20"))
    parser.add_argument("--optimize", action="store_true", help="search element lengths")
    parser.add_argument("--report", action="store_true", help="print the design report")
    parser.add_argument(
        "--feed",
        choices=("separate", "parallel", "both"),
        default="separate",
        help="separate = one driven element at a time (switched feeds); "
             "parallel = all driven elements wired to one balun",
    )
    args = parser.parse_args(argv)

    nec_dir = find_nec4nec_dir(args.nec2_dir)
    engine = NecEngine(nec_dir)
    out_dir = Path(args.out)
    design = initial_design()

    if args.optimize:
        for band in ("10m", "15m", "20m"):
            print(f"[tune] {band}", flush=True)
            design = tune_lengths(engine, design, band)
            print(f"[tune] {band} done: "
                  + ", ".join(f"{element.name}={element.length:.4f}" for element in design.by_band(band)),
                  flush=True)

    report = band_report(engine, design)
    extra: dict = {}
    if args.feed in ("parallel", "both"):
        print("[feed] modelling all driven elements in parallel to one balun", flush=True)
        extra["parallel_feed"] = band_report(engine, design, feed="parallel")
    paths = write_decks(design, out_dir)
    geometry: dict | None = None
    try:
        geometry = draw_geometry(design, out_dir / "geometry.png")
    except Exception as exc:  # matplotlib is optional
        print(f"[geometry] skipped: {type(exc).__name__}: {exc}", flush=True)
    payload = {"paths": paths, "geometry": geometry, **extra, **report}
    (out_dir / "design.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "design_summary.txt").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    for band, data in report["bands"].items():
        center = data["center"]
        print(f"{band}: f={center['freq_mhz']:.2f} MHz  swr={center['swr']:.3f}  "
              f"gain={center['gain_max_db']:.2f} dBi  fb={center['fb_db']}  "
              f"eff={center['efficiency_percent']:.2f}%  "
              f"edges swr={data['edges']}", flush=True)
    if "parallel_feed" in extra:
        print("[feed] parallel-to-one-balun results (same element lengths):", flush=True)
        for band, data in extra["parallel_feed"]["bands"].items():
            if data.get("status") == "error":
                print(f"  {band}: engine error", flush=True)
                continue
            center = data["center"]
            print(f"  {band}: f={center['freq_mhz']:.2f} MHz  swr={center['swr']:.3f}  "
                  f"gain={center['gain_max_db']:.2f} dBi  edges swr={data['edges']}", flush=True)
    print("decks:", json.dumps(paths, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
