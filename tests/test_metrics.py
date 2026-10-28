"""Tests for ``nec2core.metrics`` derived antenna metrics."""

from __future__ import annotations

import math

import pytest

from nec2core.metrics import (
    best_frequency,
    nearest_sample,
    opposite,
    peak_sample,
    return_loss_db,
    summarize_block,
    swr,
)
from nec2core.output import FrequencyBlock, PatternSample


def test_swr_and_return_loss_basics():
    assert swr(50 + 0j) == pytest.approx(1.0)
    assert swr(100 + 0j) == pytest.approx(2.0)
    # 25 ohm is |gamma| = 1/3 (SWR 2); 150 ohm is |gamma| = 1/2 (SWR 3).
    assert swr(25 + 0j) == pytest.approx(2.0)
    assert swr(150 + 0j) == pytest.approx(3.0)
    assert math.isinf(swr(0 + 0j))
    assert math.isinf(return_loss_db(50 + 0j))
    assert swr(50 + 0j, z0=75) == pytest.approx(1.5)
    # Measured 478.875 MHz feed impedance of the 8-element Yagi.
    assert swr(complex(17.2274, -19.2027)) == pytest.approx(3.379, abs=1e-3)


def test_summarize_real_block_has_all_fields(yagi_block):
    summary = summarize_block(yagi_block)
    expected = {
        "freq_mhz",
        "z_real",
        "z_imag",
        "z_magnitude",
        "swr",
        "return_loss_db",
        "feed_impedance_ohm",
        "gain_max_db",
        "gain_max_theta_deg",
        "gain_max_phi_deg",
        "front_gain_db",
        "rear_gain_db",
        "fb_db",
        "polarization",
        "efficiency_percent",
        "input_power_w",
        "radiated_power_w",
        "cuts",
    }
    assert expected <= set(summary)
    assert summary["freq_mhz"] == pytest.approx(478.875, abs=0.01)
    assert summary["z_real"] == pytest.approx(17.227, abs=0.01)
    assert summary["z_imag"] == pytest.approx(-19.203, abs=0.01)
    assert summary["swr"] == pytest.approx(3.379, abs=0.01)
    assert summary["gain_max_db"] == pytest.approx(13.47, abs=0.01)
    assert (summary["gain_max_theta_deg"], summary["gain_max_phi_deg"]) == pytest.approx((0.0, 90.0))
    assert summary["fb_db"] == pytest.approx(22.53, abs=0.2)
    assert summary["rear_gain_db"] == pytest.approx(-9.06, abs=0.2)
    assert summary["efficiency_percent"] == pytest.approx(100.0, abs=0.5)
    assert summary["polarization"] == "vertical"


def test_summary_contains_cuts(yagi_block):
    summary = summarize_block(yagi_block)
    kinds = {cut["kind"] for cut in summary["cuts"]}
    assert "elevation" in kinds
    elevation = next(cut for cut in summary["cuts"] if cut["kind"] == "elevation")
    assert elevation["fixed_deg"] == pytest.approx(90.0)
    assert elevation["peak_gain_db"] == pytest.approx(13.47, abs=0.05)
    assert "fb_db" in elevation


def test_opposite_of_polar_peak_keeps_azimuth(yagi_block):
    # Regression: peak is (theta=0, phi=90); the reverse is (180, 90), not
    # (180, 270) - at the pole the azimuth is degenerate.
    peak = peak_sample(yagi_block)
    assert peak is not None
    assert (peak.theta_deg, peak.phi_deg) == pytest.approx((0.0, 90.0))

    rear = opposite(yagi_block, peak)
    assert rear is not None
    assert rear.theta_deg == pytest.approx(180.0)
    assert rear.phi_deg == pytest.approx(90.0)
    assert rear.total_db == pytest.approx(-9.06, abs=0.2)
    assert peak.total_db - rear.total_db == pytest.approx(22.53, abs=0.2)


def test_opposite_of_equatorial_peak_flips_azimuth():
    block = FrequencyBlock(
        freq_mhz=100.0,
        patterns=[
            PatternSample(theta_deg=90.0, phi_deg=0.0, vert_db=0.0, hor_db=0.0, total_db=10.0),
            PatternSample(theta_deg=90.0, phi_deg=180.0, vert_db=0.0, hor_db=0.0, total_db=-5.0),
        ],
    )
    rear = opposite(block, block.patterns[0])
    assert rear is not None
    assert (rear.theta_deg, rear.phi_deg) == pytest.approx((90.0, 180.0))


def test_peak_sample_ignores_null_gain_marker():
    block = FrequencyBlock(
        freq_mhz=100.0,
        patterns=[
            PatternSample(theta_deg=90.0, phi_deg=0.0, vert_db=-999.99, hor_db=-999.99, total_db=-999.99),
            PatternSample(theta_deg=90.0, phi_deg=5.0, vert_db=-12.0, hor_db=-12.0, total_db=-12.0),
        ],
    )
    peak = peak_sample(block)
    assert peak is not None
    assert peak.total_db == pytest.approx(-12.0)
    assert block.max_gain_db == pytest.approx(-12.0)

    all_null = FrequencyBlock(
        freq_mhz=100.0,
        patterns=[
            PatternSample(theta_deg=90.0, phi_deg=0.0, vert_db=-999.99, hor_db=-999.99, total_db=-999.99),
        ],
    )
    assert peak_sample(all_null) is None
    assert "gain_max_db" not in summarize_block(all_null)


def test_nearest_sample_tolerance():
    block = FrequencyBlock(
        freq_mhz=100.0,
        patterns=[
            PatternSample(theta_deg=90.0, phi_deg=0.0, vert_db=0.0, hor_db=0.0, total_db=0.0),
            PatternSample(theta_deg=90.0, phi_deg=90.0, vert_db=0.0, hor_db=0.0, total_db=-10.0),
        ],
    )
    assert nearest_sample(block, 90.0, 1.0) is not None
    assert nearest_sample(block, 90.0, 45.0) is None
    assert nearest_sample(block, 90.0, 180.0) is None  # phi wraps, but no sample there


def test_best_frequency_picks_lowest_swr(parsed_yagi_out):
    best = best_frequency(parsed_yagi_out.blocks)
    assert best is not None
    assert best["freq_mhz"] == pytest.approx(478.875, abs=0.01)
    assert best["swr"] == pytest.approx(3.379, abs=0.01)
