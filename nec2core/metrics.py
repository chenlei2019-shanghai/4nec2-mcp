"""Derived antenna metrics computed from parsed NEC-2 output."""

from __future__ import annotations

import math
import re
from typing import Iterable

from .output import FrequencyBlock, PatternSample

DEFAULT_Z0 = 50.0
_NULL_GAIN = -999.99


def reflection_coefficient(z: complex, z0: float = DEFAULT_Z0) -> complex:
    return (z - z0) / (z + z0) if z0 else 0j


def swr(z: complex, z0: float = DEFAULT_Z0) -> float:
    gamma = abs(reflection_coefficient(z, z0))
    if gamma >= 1.0:
        return float("inf")
    return (1.0 + gamma) / (1.0 - gamma)


def return_loss_db(z: complex, z0: float = DEFAULT_Z0) -> float:
    gamma = abs(reflection_coefficient(z, z0))
    if gamma <= 0:
        return float("inf")
    return -20.0 * math.log10(gamma)


def _phi_delta(a: float, b: float) -> float:
    d = abs((a - b) % 360.0)
    return min(d, 360.0 - d)


def nearest_sample(
    block: FrequencyBlock,
    theta: float,
    phi: float,
    tol: float = 2.0,
) -> PatternSample | None:
    best: PatternSample | None = None
    best_score = float("inf")
    for sample in block.patterns:
        d_theta = abs(sample.theta_deg - theta)
        d_phi = _phi_delta(sample.phi_deg, phi)
        if d_theta <= tol and d_phi <= tol:
            score = d_theta + d_phi
            if score < best_score:
                best, best_score = sample, score
    return best


def peak_sample(block: FrequencyBlock) -> PatternSample | None:
    candidates = [s for s in block.patterns if s.total_db > _NULL_GAIN + 1]
    if not candidates:
        return None
    return max(candidates, key=lambda s: s.total_db)


def opposite(block: FrequencyBlock, sample: PatternSample, tol: float = 2.0) -> PatternSample | None:
    """Gain sample in the exact reverse direction.

    The reverse of (theta, phi) is (180-theta, phi+180).  At the poles
    (theta ~ 0 or 180) the azimuth is degenerate: every phi is the same
    direction, so the reverse keeps phi instead of adding 180.
    """
    theta = 180.0 - sample.theta_deg
    if theta > 180.0:
        theta -= 360.0
    if sample.theta_deg <= tol or sample.theta_deg >= 180.0 - tol:
        phi = sample.phi_deg
    else:
        phi = (sample.phi_deg + 180.0) % 360.0
    return nearest_sample(block, theta, phi, tol=tol)


def _planes(block: FrequencyBlock) -> list[dict]:
    """Group pattern samples into azimuth (fixed theta) and elevation (fixed phi) cuts."""
    by_theta: dict[float, list[PatternSample]] = {}
    by_phi: dict[float, list[PatternSample]] = {}
    for sample in block.patterns:
        by_theta.setdefault(round(sample.theta_deg, 2), []).append(sample)
        by_phi.setdefault(round(sample.phi_deg, 2), []).append(sample)

    cuts: list[dict] = []
    for theta, samples in by_theta.items():
        if len(samples) >= 8 and _span([s.phi_deg for s in samples]) >= 90.0:
            cut = _analyse_cut(
                kind="azimuth",
                fixed_deg=theta,
                varying=[(s.phi_deg, s.total_db) for s in samples],
            )
            if cut:
                cuts.append(cut)
    for phi, samples in by_phi.items():
        if len(samples) >= 8 and _span([s.theta_deg for s in samples]) >= 45.0:
            cut = _analyse_cut(
                kind="elevation",
                fixed_deg=phi,
                varying=[(s.theta_deg, s.total_db) for s in samples],
            )
            if cut:
                cuts.append(cut)
    return cuts


def _span(values: Iterable[float]) -> float:
    values = list(values)
    if not values:
        return 0.0
    return max(values) - min(values)


def _analyse_cut(kind: str, fixed_deg: float, varying: list[tuple[float, float]]) -> dict | None:
    points = sorted((a, g) for a, g in varying if g > _NULL_GAIN + 1)
    if len(points) < 8:
        return None
    peak_angle, peak_gain = max(points, key=lambda item: item[1])
    cut = {
        "kind": kind,
        "fixed_deg": fixed_deg,
        "peak_gain_db": round(peak_gain, 2),
        "peak_angle_deg": round(peak_angle, 2),
        "samples": len(points),
    }
    rear = None
    if kind == "azimuth":
        target = (peak_angle + 180.0) % 360.0
    else:
        target = 180.0 - peak_angle
    span = max(a for a, _ in points) - min(a for a, _ in points)
    for angle, gain in points:
        if _phi_delta(angle, target) <= max(1.0, span / max(1, len(points) - 1)):
            rear = gain
            break
    if rear is not None:
        cut["rear_gain_db"] = round(rear, 2)
        cut["fb_db"] = round(peak_gain - rear, 2)
    width = _beamwidth(points, peak_angle, peak_gain)
    if width is not None:
        cut["beamwidth_3db_deg"] = round(width, 1)
    return cut


def _beamwidth(points: list[tuple[float, float]], peak_angle: float, peak_gain: float) -> float | None:
    threshold = peak_gain - 3.0
    angles = [a for a, _ in points]
    if not angles:
        return None
    # Walk outward from the peak in both directions until the gain drops below -3 dB.
    ordered = sorted(points)
    idx = min(range(len(ordered)), key=lambda i: abs(ordered[i][0] - peak_angle))
    left = None
    for i in range(idx, -1, -1):
        if ordered[i][1] <= threshold:
            left = ordered[i][0]
            break
    right = None
    for i in range(idx, len(ordered)):
        if ordered[i][1] <= threshold:
            right = ordered[i][0]
            break
    if left is None or right is None:
        return None
    width = abs(right - left)
    if width <= 0:
        return None
    return width


def summarize_block(block: FrequencyBlock, z0: float = DEFAULT_Z0) -> dict:
    """Return a JSON-friendly summary for one frequency block."""
    result: dict = {"freq_mhz": round(block.freq_mhz, 4) if block.freq_mhz else None}

    driver = block.driver
    if driver:
        result.update(
            z_real=round(driver.z_real, 3),
            z_imag=round(driver.z_imag, 3),
            z_magnitude=round(abs(driver.z), 3),
            swr=round(swr(driver.z, z0), 3),
            return_loss_db=round(return_loss_db(driver.z, z0), 3),
            feed_impedance_ohm=z0,
        )
        if len(block.impedances) > 1:
            result["all_impedances"] = [
                {
                    "tag": imp.tag,
                    "segment": imp.segment,
                    "z_real": round(imp.z_real, 3),
                    "z_imag": round(imp.z_imag, 3),
                }
                for imp in block.impedances
            ]

    peak = peak_sample(block)
    if peak:
        result.update(
            gain_max_db=round(peak.total_db, 2),
            gain_max_theta_deg=round(peak.theta_deg, 2),
            gain_max_phi_deg=round(peak.phi_deg, 2),
        )
        rear = opposite(block, peak)
        if rear is not None:
            result.update(
                front_gain_db=round(peak.total_db, 2),
                rear_gain_db=round(rear.total_db, 2),
                fb_db=round(peak.total_db - rear.total_db, 2),
            )
        if peak.vert_db > _NULL_GAIN + 1 or peak.hor_db > _NULL_GAIN + 1:
            result["polarization"] = "vertical" if peak.vert_db > peak.hor_db else "horizontal"

    if block.efficiency_percent is not None:
        result["efficiency_percent"] = round(block.efficiency_percent, 2)
    if block.input_power_w is not None:
        result["input_power_w"] = round(block.input_power_w, 6)
    if block.radiated_power_w is not None:
        result["radiated_power_w"] = round(block.radiated_power_w, 6)
    if block.average_power_gain is not None:
        result["average_power_gain"] = round(block.average_power_gain, 5)

    cuts = _planes(block)
    if cuts:
        result["cuts"] = cuts

    if block.errors:
        result["errors"] = block.errors
    if block.warnings:
        result["warnings"] = block.warnings
    return result


def best_frequency(blocks: list[FrequencyBlock], z0: float = DEFAULT_Z0) -> dict | None:
    """Pick the frequency with the lowest SWR, tie-broken by highest gain."""
    rows = [summarize_block(b, z0) for b in blocks]
    rows = [r for r in rows if r.get("swr") is not None]
    if not rows:
        return None
    return min(rows, key=lambda r: (r["swr"], -(r.get("gain_max_db") or -999)))
