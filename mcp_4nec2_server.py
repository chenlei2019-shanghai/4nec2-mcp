#!/usr/bin/env python3
"""4NEC2 MCP server.

Exposes NEC-2 antenna simulation (via the engines shipped with 4NEC2) as MCP
tools so DeepSeek Harness and other MCP clients can run models, sweep
frequencies, build Yagi designs and optimise geometry.

Entry point:
    python mcp_4nec2_server.py --4nec2-dir C:\\4nec2
    python mcp_4nec2_server.py --transport streamable-http --port 8765

Transport defaults to stdio, which is what DSH's built-in MCP client uses.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nec2core import (  # noqa: E402
    NecEngine,
    NecError,
    best_frequency,
    describe_model,
    find_nec4nec_dir,
    list_engines,
    parse_deck,
    parse_output,
    set_frequency,
    set_single_frequency,
    summarize_block,
)
from nec2core.metrics import DEFAULT_Z0  # noqa: E402

SERVER_NAME = "4nec2"
SERVER_VERSION = "2.1.0"

DEFAULT_MODEL_DIRS = (Path(r"C:\4nec2\models"), Path(r"C:\4nec2\necfile"), Path(r"C:\4nec2"))

#: Where rendered figures go when the caller does not pass ``out_dir``.
DEFAULT_PLOTS_DIR = Path(
    os.environ.get("NEC_PLOTS_DIR") or (Path(__file__).resolve().parent / "plots")
)


def _read_text(path: Path) -> str:
    """Read a NEC file, tolerating the GBK/ANSI encodings used by 4NEC2 files."""
    data = path.read_bytes()
    for encoding in ("utf-8", "gbk", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _resolve_path(path: str | None, *, must_exist: bool = True) -> Path | None:
    """Resolve a user-supplied path (absolute, workspace-relative or models-relative)."""
    if not path:
        return None
    candidate = Path(path).expanduser()
    if candidate.is_file():
        return candidate
    if not candidate.is_absolute():
        search_dirs = [Path.cwd(), *DEFAULT_MODEL_DIRS, Path(r"C:\4nec2-models")]
        for base in search_dirs:
            for probe in (base / candidate, base / candidate.name):
                if probe.is_file():
                    return probe
    if must_exist:
        raise FileNotFoundError(f"NEC file not found: {path}")
    return candidate


def _load_deck(path: str | None, deck: str | None) -> tuple[str, str]:
    """Return (deck_text, source_label) from either a path or an inline deck."""
    if deck:
        return deck, "<inline>"
    resolved = _resolve_path(path)
    if resolved is None:
        raise ValueError("either 'path' or 'deck' is required")
    return _read_text(resolved), str(resolved)


def _engine(nec4nec_dir: str | None) -> NecEngine:
    return NecEngine(find_nec4nec_dir(nec4nec_dir))


def _err(message: str, *, errors: list[str] | None = None, **extra) -> dict:
    payload = {"status": "error", "error": message}
    if errors:
        payload["errors"] = errors
    payload.update(extra)
    return payload


def _plots_dir(out_dir: str | None) -> Path:
    """Resolve (and create) the directory that receives rendered figures."""
    directory = Path(out_dir).expanduser() if out_dir else DEFAULT_PLOTS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _preload_plotting() -> dict:
    """Prime numpy/matplotlib in the main thread before the transport starts.

    Importing numpy's C extension for the first time inside an anyio worker
    thread deadlocks on Windows, which makes every ``nec_plot_*`` tool call hang
    until the MCP client cancels it.  Doing the import here keeps plotting
    responsive; set ``NEC_PLOTS_PRELOAD=0`` to skip it (tools then import
    lazily inside the worker thread and may hang).
    """
    disabled = os.environ.get("NEC_PLOTS_PRELOAD", "1").strip().lower() in {"0", "false", "no", "off"}
    if disabled:
        return {"ok": False, "skipped": True}
    from nec2core import plots  # local import keeps --version/--help cheap

    info = plots.preload()
    if not info.get("ok") and not info.get("skipped"):
        print(f"[4nec2-mcp] plotting unavailable: {info.get('error')}", file=sys.stderr)
    return info


def _slug(text: str) -> str:
    stem = Path(text).stem if text else "inline"
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in stem)
    return cleaned.strip("_") or "inline"


def _figure(path: Path, kind: str, **extra) -> dict:
    payload = {"kind": kind, "path": str(path), "size_bytes": path.stat().st_size}
    payload.update(extra)
    return payload


def _deck_segments(deck: str) -> int | None:
    """Segment count straight from the deck, used when the output omits it."""
    try:
        return parse_deck(deck).total_segments or None
    except Exception:  # noqa: BLE001 - never fail a run over a cosmetic field
        return None


def _run_summary(result, z0: float, include_pattern: bool = False) -> dict:
    parsed = parse_output(result.output)
    if not parsed.blocks:
        return {
            "status": "error",
            "engine": result.engine_name,
            "duration_s": round(result.duration_s, 3),
            "errors": result.errors or ["no frequency block found in the engine output"],
            "warnings": result.warnings,
            "raw_head": result.output[:1200],
        }
    block = parsed.primary
    summary = summarize_block(block, z0)
    summary.update(
        status="ok",
        engine=result.engine_name,
        duration_s=round(result.duration_s, 3),
        segments=parsed.segment_count or _deck_segments(result.deck),
        source=result.saved_out_path or "temp",
    )
    if result.warnings:
        summary["engine_warnings"] = result.warnings
    if include_pattern and block.patterns:
        summary["pattern"] = [
            {
                "theta_deg": round(s.theta_deg, 2),
                "phi_deg": round(s.phi_deg, 2),
                "total_db": round(s.total_db, 2),
            }
            for s in block.patterns
            if s.total_db > -900
        ]
    return summary


def build_server(nec4nec_dir: str | None = None, host: str = "127.0.0.1", port: int = 8765) -> FastMCP:
    """Create the FastMCP instance with all 4NEC2 tools registered."""
    mcp = FastMCP(
        SERVER_NAME,
        instructions=(
            "4NEC2/NEC-2 antenna simulation. Paths may be absolute or relative to "
            r"C:\4nec2\models. 'nec_run' runs one NEC deck; 'nec_sweep' runs a "
            "frequency sweep; 'nec_describe' inspects a .nec file; 'nec_build_yagi' "
            "generates a Yagi model; 'nec_optimize' tunes geometry; "
            "'nec_plot_pattern' and 'nec_plot_sweep' render PNG radiation patterns "
            "and SWR/gain/impedance curves. All metrics are "
            "in dBi and ohms; SWR assumes the feed impedance given by z0 (default 50). "
            "Before designing a NEW antenna, confirm requirements with the user first "
            "(see docs/设计需求清单.md): bands/center frequencies, bandwidth and SWR "
            "target, element count and boom length limit, full-size vs loaded, element "
            "tube diameter (uniform or tapered sections), and for MULTI-BAND antennas "
            "the feed topology — separate feedlines per band, all driven elements "
            "hardwired in parallel to one balun, open-sleeve, or trapped — plus power "
            "rating. The feed topology changes the design fundamentally."
        ),
        host=host,
        port=port,
    )
    # FastMCP has no ``version`` kwarg in mcp 1.x, so the low-level server
    # would report the installed mcp package version instead of ours.
    mcp._mcp_server.version = SERVER_VERSION

    @mcp.tool()
    def nec_list_engines() -> dict:
        """List the NEC-2 engines found in the 4NEC2 installation.

        Returns the installation directory, every usable engine executable and
        which one is preferred by default. Use it to diagnose missing engines.
        """
        try:
            base = find_nec4nec_dir(nec4nec_dir)
            engines = list_engines(base)
            preferred = next((e["path"] for e in engines if e["preferred"]), None)
            return {"status": "ok", "nec4nec_dir": str(base), "preferred": preferred, "engines": engines}
        except Exception as exc:  # noqa: BLE001 - surfaced to the model
            return _err(str(exc))

    @mcp.tool()
    def nec_list_models(directory: str | None = None, pattern: str = "*.nec", limit: int = 200) -> dict:
        """List NEC model files (*.nec) available for simulation.

        Args:
            directory: folder to scan; defaults to C:\\4nec2\\models then C:\\4nec2\\necfile.
            pattern: glob pattern (e.g. "*.nec" or "*480*").
            limit: maximum number of files returned.
        """
        try:
            roots = [Path(directory)] if directory else [p for p in DEFAULT_MODEL_DIRS if p.is_dir()]
            seen: list[dict] = []
            for root in roots:
                if not root.is_dir():
                    continue
                for path in sorted(root.glob(pattern)):
                    if not path.is_file():
                        continue
                    seen.append(
                        {
                            "path": str(path),
                            "name": path.name,
                            "size_bytes": path.stat().st_size,
                            "modified": path.stat().st_mtime,
                        }
                    )
                    if len(seen) >= limit:
                        break
                if len(seen) >= limit:
                    break
            return {"status": "ok", "count": len(seen), "files": seen}
        except Exception as exc:  # noqa: BLE001
            return _err(str(exc))

    @mcp.tool()
    def nec_describe(path: str, include_cards: bool = False) -> dict:
        """Describe a .nec model: frequency, ground, wires, element geometry, SY symbols.

        Use this before running a model to check that it is well formed and to
        understand element lengths/spacings. Does not execute the engine.

        Args:
            path: path to the .nec file.
            include_cards: include the raw card list when true.
        """
        try:
            resolved = _resolve_path(path)
            model = parse_deck(_read_text(resolved))
            description = describe_model(model)
            description.update(status="ok", path=str(resolved), symbol_count=len(model.symbols))
            if include_cards:
                description["cards"] = [
                    {"label": c.label, "fields": c.fields, "raw": c.raw} for c in model.cards
                ]
            return description
        except Exception as exc:  # noqa: BLE001
            return _err(str(exc))

    @mcp.tool()
    def nec_run(
        path: str | None = None,
        deck: str | None = None,
        freq_mhz: float | None = None,
        engine: str | None = None,
        z0: float = DEFAULT_Z0,
        timeout_s: float = 120.0,
        save_out_path: str | None = None,
        include_pattern: bool = False,
    ) -> dict:
        """Run one NEC-2 deck and return impedance, SWR, peak gain and F/B.

        Args:
            path: .nec file to run (absolute or relative to C:\\4nec2\\models).
            deck: inline NEC deck text; use instead of path.
            freq_mhz: override the model frequency before running.
            engine: engine executable (name or path); default is nec2dxs11k.exe.
            z0: feed impedance in ohms used for SWR (default 50).
            timeout_s: per-run engine timeout.
            save_out_path: write the raw NEC output next to this path.
            include_pattern: include every radiation-pattern sample.
        """
        try:
            text, source = _load_deck(path, deck)
            if freq_mhz:
                text = set_single_frequency(text, freq_mhz)
            result = _engine(nec4nec_dir).run(text, timeout_s=timeout_s, save_out_path=save_out_path)
            if not result.ok and not result.output:
                return _err(
                    "engine failed",
                    errors=result.errors,
                    engine=result.engine_name,
                    stdout=result.stdout[-800:],
                )
            summary = _run_summary(result, float(z0), include_pattern=include_pattern)
            summary["model"] = source
            if freq_mhz:
                summary["frequency_override_mhz"] = freq_mhz
            return summary
        except Exception as exc:  # noqa: BLE001
            return _err(str(exc))

    @mcp.tool()
    def nec_sweep(
        path: str | None = None,
        deck: str | None = None,
        start_mhz: float | None = None,
        stop_mhz: float | None = None,
        step_mhz: float = 1.0,
        freqs_mhz: list[float] | None = None,
        engine: str | None = None,
        z0: float = DEFAULT_Z0,
        timeout_s: float = 180.0,
    ) -> dict:
        """Sweep frequency and return impedance/SWR/gain/F-B for each point.

        Uses a single multi-frequency NEC run when the sweep is uniform, falling
        back to one engine run per frequency otherwise.

        Args:
            path: .nec file (absolute or relative to C:\\4nec2\\models).
            deck: inline NEC deck text; use instead of path.
            start_mhz/stop_mhz/step_mhz: uniform sweep definition.
            freqs_mhz: explicit frequency list (overrides start/stop/step).
            engine: engine executable (name or path).
            z0: feed impedance for SWR.
            timeout_s: timeout for the whole sweep.
        """
        try:
            text, source = _load_deck(path, deck)
            freqs = _sweep_frequencies(start_mhz, stop_mhz, step_mhz, freqs_mhz)
            if not freqs:
                return _err("no frequencies given: pass start_mhz/stop_mhz or freqs_mhz")
            engine_obj = _engine(nec4nec_dir)
            rows = _run_sweep(engine_obj, text, freqs, z0=float(z0), timeout_s=timeout_s)
            ok_rows = [r for r in rows if r.get("status") == "ok"]
            if not ok_rows:
                return _err("every sweep point failed", rows=rows)
            return {
                "status": "ok",
                "model": source,
                "points": len(ok_rows),
                "requested_points": len(freqs),
                "rows": rows,
                "best_swr": min(ok_rows, key=lambda r: r.get("swr", 9e9)),
                "best_gain": max(ok_rows, key=lambda r: r.get("gain_max_db", -9e9)),
                "bandwidth_swr2_mhz": _swr_bandwidth(ok_rows, 2.0),
            }
        except Exception as exc:  # noqa: BLE001
            return _err(str(exc))

    @mcp.tool()
    def nec_plot_pattern(
        path: str | None = None,
        deck: str | None = None,
        freq_mhz: float | None = None,
        plane: str = "both",
        include_3d: bool = False,
        phi_deg: float = 90.0,
        theta_deg: float = 90.0,
        column: str = "total",
        out_dir: str | None = None,
        engine: str | None = None,
        z0: float = DEFAULT_Z0,
        timeout_s: float = 120.0,
        dpi: int = 140,
    ) -> dict:
        """Render radiation-pattern figures (PNG) for one frequency.

        Runs the deck once and writes polar cuts of the gain pattern, plus an
        optional 3-D lobe. Returns the image paths so a client can display them.

        Args:
            path: .nec file (absolute or relative to C:\\4nec2\\models).
            deck: inline NEC deck text; use instead of path.
            freq_mhz: override the model frequency before running.
            plane: 'both' (elevation + azimuth), 'elevation' or 'azimuth'.
            include_3d: replace the RP cards with a full sphere grid and render
                a 3-D lobe (needs matplotlib; slower but still < 1 s here).
            phi_deg: fixed phi for the elevation cut (default 90).
            theta_deg: fixed theta for the azimuth cut (default 90).
            column: 'total', 'vertical' or 'horizontal' gain column.
            out_dir: directory for the PNGs (default C:\\4nec2-mcp-server\\plots).
            engine: engine executable (name or path).
            z0: feed impedance for the summary metrics.
            timeout_s: engine timeout.
            dpi: PNG resolution.
        """
        try:
            from nec2core import plots as plotting
        except Exception as exc:  # noqa: BLE001
            return _err("plotting needs matplotlib: pip install matplotlib", errors=[str(exc)])
        if not plotting.matplotlib_available():
            return _err("plotting needs matplotlib: pip install matplotlib")
        if plane not in {"both", "elevation", "azimuth"}:
            return _err("plane must be 'both', 'elevation' or 'azimuth'")
        if column not in {"total", "vertical", "horizontal"}:
            return _err("column must be 'total', 'vertical' or 'horizontal'")
        try:
            text, source = _load_deck(path, deck)
            notes: list[str] = []
            if include_3d:
                text = plotting.with_sphere_grid(text)
                notes.append("RP 卡已替换为完整球面网格（37×73，5° 步进）以绘制 3-D 方向图。")
            if freq_mhz:
                text = set_single_frequency(text, freq_mhz)
            result = _engine(nec4nec_dir).run(text, timeout_s=timeout_s)
            parsed = parse_output(result.output)
            if not parsed.blocks:
                return _err(
                    "engine produced no frequency block",
                    errors=result.errors,
                    engine=result.engine_name,
                )
            block = parsed.blocks[0]
            output_dir = _plots_dir(out_dir)
            basename = f"{_slug(source)}_{block.freq_mhz:.3f}MHz"
            planes = ("elevation", "azimuth") if plane == "both" else (plane,)
            figures: list[dict] = []
            polar = plotting.polar_pattern(
                block,
                output_dir / f"{basename}_polar.png",
                planes=planes,
                phi_deg=phi_deg,
                theta_deg=theta_deg,
                column=column,
                dpi=int(dpi),
            )
            figures.append(
                _figure(
                    Path(polar["path"]),
                    "polar",
                    peak_gain_db=polar["peak_gain_db"],
                    planes=polar["planes"],
                )
            )
            if include_3d:
                try:
                    lobe = plotting.pattern_3d(
                        block, output_dir / f"{basename}_3d.png", dpi=int(dpi)
                    )
                    figures.append(
                        _figure(
                            Path(lobe["path"]), "3d", peak_gain_db=lobe["peak_gain_db"]
                        )
                    )
                except ValueError as exc:  # incomplete sphere grid
                    notes.append(str(exc))
            summary = summarize_block(block, float(z0))
            return {
                "status": "ok",
                "model": source,
                "freq_mhz": block.freq_mhz,
                "peak_gain_db": summary.get("gain_max_db"),
                "figures": figures,
                "notes": notes,
                "engine": result.engine_name,
                "duration_s": round(result.duration_s, 3),
            }
        except Exception as exc:  # noqa: BLE001
            return _err(str(exc))

    @mcp.tool()
    def nec_plot_sweep(
        path: str | None = None,
        deck: str | None = None,
        start_mhz: float | None = None,
        stop_mhz: float | None = None,
        step_mhz: float = 1.0,
        freqs_mhz: list[float] | None = None,
        out_dir: str | None = None,
        engine: str | None = None,
        z0: float = DEFAULT_Z0,
        timeout_s: float = 180.0,
        dpi: int = 150,
    ) -> dict:
        """Sweep frequency and render a 4-panel curve dashboard (PNG).

        Panels: SWR, gain + front-to-back, R/X impedance, Smith chart. Returns
        the image path plus the same best_swr / best_gain / bandwidth summary as
        ``nec_sweep``.

        Args:
            path: .nec file (absolute or relative to C:\\4nec2\\models).
            deck: inline NEC deck text; use instead of path.
            start_mhz/stop_mhz/step_mhz: uniform sweep definition.
            freqs_mhz: explicit frequency list (overrides start/stop/step).
            out_dir: directory for the PNG (default C:\\4nec2-mcp-server\\plots).
            engine: engine executable (name or path).
            z0: feed impedance for SWR and the Smith chart.
            timeout_s: timeout for the whole sweep.
            dpi: PNG resolution.
        """
        try:
            from nec2core import plots as plotting
        except Exception as exc:  # noqa: BLE001
            return _err("plotting needs matplotlib: pip install matplotlib", errors=[str(exc)])
        if not plotting.matplotlib_available():
            return _err("plotting needs matplotlib: pip install matplotlib")
        try:
            text, source = _load_deck(path, deck)
            freqs = _sweep_frequencies(start_mhz, stop_mhz, step_mhz, freqs_mhz)
            if not freqs:
                return _err("no frequencies given: pass start_mhz/stop_mhz or freqs_mhz")
            rows = _run_sweep(
                _engine(nec4nec_dir), text, freqs, z0=float(z0), timeout_s=timeout_s
            )
            ok_rows = [row for row in rows if row.get("status") == "ok"]
            if not ok_rows:
                return _err("every sweep point failed", rows=rows)
            output_dir = _plots_dir(out_dir)
            png = output_dir / f"{_slug(source)}_sweep.png"
            info = plotting.sweep_dashboard(
                ok_rows,
                png,
                z0=float(z0),
                title=f"扫频曲线：{Path(source).name}",
                dpi=int(dpi),
            )
            return {
                "status": "ok",
                "model": source,
                "points": len(ok_rows),
                "frequency_range_mhz": info["freq_range_mhz"],
                "figures": [_figure(png, "sweep", points=len(ok_rows))],
                "best_swr": min(ok_rows, key=lambda row: row.get("swr", 9e9)),
                "best_gain": max(ok_rows, key=lambda row: row.get("gain_max_db", -9e9)),
                "bandwidth_swr2_mhz": _swr_bandwidth(ok_rows, 2.0),
            }
        except Exception as exc:  # noqa: BLE001
            return _err(str(exc))

    @mcp.tool()
    def nec_parse_out(path: str, z0: float = DEFAULT_Z0) -> dict:
        """Parse an existing NEC output (*.out) file without running the engine.

        Args:
            path: path to a .out file produced by 4NEC2/NEC-2.
            z0: feed impedance used for SWR.
        """
        try:
            resolved = _resolve_path(path)
            parsed = parse_output(_read_text(resolved))
            if not parsed.blocks:
                return _err("no frequency block found", errors=parsed.errors, warnings=parsed.warnings)
            return {
                "status": "ok",
                "path": str(resolved),
                "blocks": [summarize_block(b, float(z0)) for b in parsed.blocks],
                "errors": parsed.errors,
                "warnings": parsed.warnings,
            }
        except Exception as exc:  # noqa: BLE001
            return _err(str(exc))

    @mcp.tool()
    def nec_build_yagi(
        freq_mhz: float,
        n_elements: int = 8,
        boom_length_m: float | None = None,
        radius_m: float = 0.004,
        element_lengths_m: list[float] | None = None,
        element_positions_m: list[float] | None = None,
        ground: str = "free",
        z0: float = DEFAULT_Z0,
        run: bool = True,
        save_path: str | None = None,
        timeout_s: float = 120.0,
    ) -> dict:
        """Generate a Yagi-Uda model from geometry and optionally simulate it.

        Elements are vertical (along Y), the boom runs along Z, and the driven
        element (element 2, right after the reflector) is fed at its centre.

        Args:
            freq_mhz: design frequency.
            n_elements: number of elements (2 = reflector + driven).
            boom_length_m: total boom length; default 0.2 wavelength per gap
                (0.2 * (n_elements - 1) wavelengths), which is the spacing the
                classic element taper is designed for.
            radius_m: element radius in metres (0.004 = 8 mm diameter).
            element_lengths_m: explicit element lengths, reflector first.
            element_positions_m: explicit boom positions, first element first.
            ground: "free" (GN -1) or "perfect" (GN 1).
            z0: feed impedance for SWR.
            run: run the generated deck immediately.
            save_path: write the generated .nec file here.
            timeout_s: engine timeout when run is true.
        """
        try:
            from nec2core.yagi import YagiDesign, YagiElement  # local import: optional module

            if element_lengths_m and element_positions_m:
                if len(element_lengths_m) != len(element_positions_m):
                    return _err("element_lengths_m and element_positions_m must have the same length")
                elements = [
                    YagiElement(length_m=float(l), position_m=float(p))
                    for l, p in zip(element_lengths_m, element_positions_m)
                ]
            else:
                design = YagiDesign.classic(
                    freq_mhz=float(freq_mhz),
                    n_elements=int(n_elements),
                    boom_length_m=float(boom_length_m) if boom_length_m else None,
                    radius_m=float(radius_m),
                )
                elements = design.elements
            design = YagiDesign(
                freq_mhz=float(freq_mhz),
                elements=elements,
                radius_m=float(radius_m),
                ground=ground,
            )
            problems = design.validate()
            if problems:
                return _err("invalid Yagi design", problems=problems)
            deck = design.to_nec()
            if save_path:
                target = Path(save_path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(deck, encoding="utf-8")
            result: dict = {
                "status": "ok",
                "frequency_mhz": float(freq_mhz),
                "elements": [
                    {"length_m": round(e.length_m, 4), "position_m": round(e.position_m, 4)}
                    for e in design.elements
                ],
                "saved_model": str(save_path) if save_path else None,
                "deck": deck,
            }
            if run:
                run_result = _engine(nec4nec_dir).run(deck, timeout_s=timeout_s)
                result["simulation"] = _run_summary(run_result, float(z0))
            return result
        except Exception as exc:  # noqa: BLE001
            return _err(str(exc))

    @mcp.tool()
    def nec_optimize(
        path: str | None = None,
        deck: str | None = None,
        variables: list[dict] | None = None,
        objective: str = "score",
        max_evals: int = 40,
        z0: float = DEFAULT_Z0,
        engine: str | None = None,
        timeout_s: float = 240.0,
    ) -> dict:
        """Optimise geometry parameters of a model (coordinate search over SY symbols).

        Args:
            path: .nec file (may use SY symbols for the tuned dimensions).
            deck: inline deck text; use instead of path.
            variables: list of {"symbol": name, "min": lo, "max": hi, "step": s}.
            objective: "gain" (max), "fb" (max front-to-back), "swr" (min) or
                "score" (gain/directivity trade-off, default).
            max_evals: maximum number of engine runs.
            z0: feed impedance for SWR.
            engine: engine executable (name or path).
            timeout_s: overall optimisation time budget.
        """
        try:
            from nec2core.optimize import optimize_design

            text, source = _load_deck(path, deck)
            if not variables:
                return _err("variables are required, e.g. [{'symbol': 'Ld1', 'min': 0.23, 'max': 0.27}]")
            outcome = optimize_design(
                deck=text,
                variables=variables,
                objective=objective,
                engine=_engine(nec4nec_dir),
                max_evals=int(max_evals),
                z0=float(z0),
                timeout_s=float(timeout_s),
            )
            outcome["status"] = "ok"
            outcome["model"] = source
            return outcome
        except Exception as exc:  # noqa: BLE001
            return _err(str(exc))

    return mcp


# ----------------------------------------------------------------------------
# Sweep helpers
# ----------------------------------------------------------------------------
def _sweep_frequencies(
    start_mhz: float | None,
    stop_mhz: float | None,
    step_mhz: float,
    freqs_mhz: list[float] | None,
) -> list[float]:
    if freqs_mhz:
        return sorted({round(float(f), 6) for f in freqs_mhz})
    if start_mhz is None or stop_mhz is None:
        return []
    start, stop = float(start_mhz), float(stop_mhz)
    step = abs(float(step_mhz)) or 1.0
    if stop < start:
        start, stop = stop, start
    count = int(round((stop - start) / step))
    freqs = [round(start + i * step, 6) for i in range(count + 1)]
    if freqs and abs(freqs[-1] - stop) > 1e-6 and stop - freqs[-1] < step:
        freqs.append(round(stop, 6))
    return freqs


def _patterns_are_complete(block) -> bool:
    """True when the block holds a pattern cut that can contain the peak.

    With several RP cards a multi-frequency run only prints the first cut for
    every frequency and the remaining cuts once for the last frequency, so a
    block whose samples share a single theta (an azimuth-only cut) cannot be
    used to report peak gain.
    """
    thetas = {round(s.theta_deg, 3) for s in block.patterns}
    if len(thetas) < 2:
        return False
    return True


def _run_sweep(engine_obj: NecEngine, deck: str, freqs: list[float], *, z0: float, timeout_s: float) -> list[dict]:
    """Run a sweep, preferring one multi-frequency NEC run when possible."""
    rows: list[dict] = []
    steps = [round(b - a, 6) for a, b in zip(freqs, freqs[1:])]
    uniform = not steps or all(abs(s - steps[0]) < 1e-6 for s in steps)
    if uniform:
        try:
            sweep_deck = set_frequency(deck, freqs) if len(freqs) > 1 else set_single_frequency(deck, freqs[0])
            result = engine_obj.run(sweep_deck, timeout_s=timeout_s)
            parsed = parse_output(result.output)
            if len(parsed.blocks) == len(freqs) and all(_patterns_are_complete(b) for b in parsed.blocks):
                return [{"status": "ok", **_row(summarize_block(block, z0))} for block in parsed.blocks]
        except (NecError, Exception):  # noqa: BLE001 - fall back to per-frequency runs
            pass

    for freq in freqs:
        try:
            result = engine_obj.run(set_single_frequency(deck, freq), timeout_s=timeout_s)
            parsed = parse_output(result.output)
            if not parsed.blocks:
                rows.append({"status": "error", "freq_mhz": freq, "errors": result.errors})
                continue
            rows.append({"status": "ok", **_row(summarize_block(parsed.blocks[0], z0))})
        except Exception as exc:  # noqa: BLE001
            rows.append({"status": "error", "freq_mhz": freq, "errors": [str(exc)]})
    return rows


def _row(summary: dict) -> dict:
    keys = (
        "freq_mhz",
        "z_real",
        "z_imag",
        "swr",
        "gain_max_db",
        "fb_db",
        "efficiency_percent",
    )
    return {k: summary.get(k) for k in keys if k in summary}


def _swr_bandwidth(rows: list[dict], limit: float) -> dict | None:
    below = [r["freq_mhz"] for r in rows if r.get("swr") is not None and r["swr"] <= limit]
    if len(below) < 2:
        return None
    return {"swr_limit": limit, "low_mhz": min(below), "high_mhz": max(below), "width_mhz": round(max(below) - min(below), 3)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="4NEC2 MCP server")
    parser.add_argument("--4nec2-dir", dest="nec4nec_dir", default=os.environ.get("NEC4NEC_DIR") or r"C:\4nec2")
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--version", action="version", version=SERVER_VERSION)
    parser.add_argument(
        "--no-plots",
        action="store_true",
        help="do not preload matplotlib (nec_plot_* tools may then hang inside the worker thread)",
    )
    args = parser.parse_args(argv)

    if args.no_plots:
        os.environ["NEC_PLOTS_PRELOAD"] = "0"
    plot_info = _preload_plotting()
    if plot_info.get("ok"):
        print(
            f"[4nec2-mcp] matplotlib {plot_info.get('matplotlib')} ready in {plot_info.get('seconds')}s",
            file=sys.stderr,
        )

    mcp = build_server(args.nec4nec_dir, host=args.host, port=args.port)
    try:
        mcp.run(transport=args.transport)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
