"""Offline tests for :mod:`nec2core.plots` (matplotlib rendering).

These never start the NEC engine: the real fixture output drives the polar plot,
and a synthetic spherical grid drives the 3-D plot.  The stdio regression test
for the "first numpy import in a worker thread deadlocks" bug lives in
``tests/test_server_tools.py`` because it needs the real MCP transport.
"""

from __future__ import annotations

from pathlib import Path

import pytest

matplotlib = pytest.importorskip("matplotlib")

from nec2core.output import FrequencyBlock, PatternSample  # noqa: E402
from nec2core import plots  # noqa: E402


def test_preload_uses_the_agg_backend():
    info = plots.preload()
    assert info["ok"] is True, info
    assert info["backend"].lower() == "agg"
    assert info["matplotlib"] == matplotlib.__version__


def test_polar_pattern_renders_the_fixture_block(yagi_block, tmp_path: Path):
    out = tmp_path / "polar.png"
    info = plots.polar_pattern(yagi_block, out)

    assert out.exists()
    assert out.stat().st_size > 20_000
    assert info["freq_mhz"] == pytest.approx(478.88, abs=0.01)
    assert info["peak_gain_db"] == pytest.approx(13.47, abs=0.05)
    assert info["planes"] == ["elevation", "azimuth"]
    assert info["elevation_fixed_phi_deg"] == pytest.approx(90.0)


def test_polar_pattern_rejects_unknown_planes(yagi_block, tmp_path: Path):
    with pytest.raises(ValueError, match="planes"):
        plots.polar_pattern(yagi_block, tmp_path / "nope.png", planes=("sideways",))


def test_pattern_3d_needs_a_full_sphere(yagi_block, tmp_path: Path):
    with pytest.raises(ValueError, match="球面网格"):
        plots.pattern_3d(yagi_block, tmp_path / "lobe.png")


def _synthetic_sphere(freq_mhz: float = 470.0) -> FrequencyBlock:
    patterns = []
    for theta in range(0, 181, 10):
        for phi in range(0, 360, 10):
            # Broadside lobe around theta = 90: a plausible hand-made pattern.
            gain = 12.0 - 12.0 * (abs(theta - 90) / 90.0) ** 2
            patterns.append(
                PatternSample(
                    theta_deg=float(theta),
                    phi_deg=float(phi),
                    vert_db=gain,
                    hor_db=gain - 1.0,
                    total_db=gain,
                )
            )
    return FrequencyBlock(freq_mhz=freq_mhz, patterns=patterns)


def test_pattern_3d_renders_a_synthetic_sphere(tmp_path: Path):
    out = tmp_path / "lobe.png"
    info = plots.pattern_3d(_synthetic_sphere(), out)

    assert out.exists()
    assert out.stat().st_size > 5_000
    assert info["peak_gain_db"] == pytest.approx(12.0)


def test_sweep_dashboard_renders_without_engine(tmp_path: Path):
    rows = [
        {
            "freq_mhz": float(freq),
            "swr": 1.2 + abs(freq - 480.0) / 25.0,
            "gain_max_db": 13.5 - abs(freq - 485.0) / 20.0,
            "fb_db": 22.0,
            "z_real": 45.0,
            "z_imag": (freq - 480.0) * 0.4,
            "efficiency_percent": 99.9 - abs(freq - 480.0) / 30.0,
        }
        for freq in range(470, 491, 2)
    ]
    out = tmp_path / "sweep.png"
    info = plots.sweep_dashboard(rows, out)

    assert out.exists()
    assert out.stat().st_size > 20_000
    assert info["points"] == len(rows)
    assert info["freq_range_mhz"] == [470.0, 490.0]

    # fifth panel: efficiency (%) versus frequency
    efficiency = info["efficiency_percent"]
    assert isinstance(efficiency["min"], float)
    assert isinstance(efficiency["max"], float)
    assert efficiency["min"] == pytest.approx(99.9 - 10.0 / 30.0, abs=1e-6)
    assert efficiency["max"] == pytest.approx(99.9, abs=1e-6)
    assert efficiency["min"] <= efficiency["max"] <= 100.0


def test_sweep_dashboard_survives_missing_efficiency(tmp_path: Path):
    rows = [
        {
            "freq_mhz": float(freq),
            "swr": 1.3 + abs(freq - 480.0) / 30.0,
            "gain_max_db": 13.0 - abs(freq - 485.0) / 25.0,
            "fb_db": None,
            "z_real": 50.0,
            "z_imag": (freq - 480.0) * 0.3,
            "efficiency_percent": None,
        }
        for freq in range(472, 489, 2)
    ]
    out = tmp_path / "sweep_no_efficiency.png"
    info = plots.sweep_dashboard(rows, out)

    assert out.exists()
    assert out.stat().st_size > 20_000
    assert info["points"] == len(rows)
    assert info["efficiency_percent"] == {"min": None, "max": None}


def test_sweep_dashboard_rejects_empty_rows(tmp_path: Path):
    with pytest.raises(ValueError, match="no sweep rows"):
        plots.sweep_dashboard([], tmp_path / "empty.png")


def _elevation_curve(peak_db: float, takeoff_deg: float) -> dict:
    return {
        "label": f"test {takeoff_deg:g}°",
        "color": "#2e7d32",
        "pairs": [
            (angle, peak_db - ((angle - takeoff_deg) / 12.0) ** 2)
            for angle in range(0, 91, 2)
        ],
    }


def test_elevation_comparison_renders_both_panels(tmp_path: Path):
    curves = [
        _elevation_curve(7.0, 0.0),
        _elevation_curve(11.3, 27.0),
        _elevation_curve(12.3, 15.0),
    ]
    out = tmp_path / "elevation_compare.png"
    info = plots.elevation_comparison_polar(curves, out, title="test")

    assert out.exists()
    assert out.stat().st_size > 20_000
    assert info["curves"] == ["test 0°", "test 27°", "test 15°"]
    assert info["peak_gain_db"] == pytest.approx(12.3, abs=0.1)


def test_elevation_comparison_rejects_curves_without_samples(tmp_path: Path):
    with pytest.raises(ValueError, match="no elevation samples"):
        plots.elevation_comparison_polar(
            [{"label": "empty", "pairs": []}], tmp_path / "empty_elevation.png"
        )
