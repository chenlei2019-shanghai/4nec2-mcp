"""Parser for NEC-2 output files (*.out) produced by the 4NEC2 engines.

NEC-2 prints one section per frequency.  Values can be glued together when a
number is negative (``1.92595E+01-1.35152E+01``), so numeric extraction uses a
token regex instead of ``str.split`` on the impedance rows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

FLOAT_TOKEN = re.compile(r"[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[EeDd][+-]?\d+)?")
FREQ_RE = re.compile(r"FREQUENCY=\s*([-+]?[\d.]+(?:[Ee][-+]?\d+)?)\s*MHZ")
POWER_RE = re.compile(
    r"(INPUT POWER|RADIATED POWER|STRUCTURE LOSS|NETWORK LOSS|EFFICIENCY)\s*=\s*([-+]?[\d.]+(?:[Ee][-+]?\d+)?)",
    re.I,
)
AVG_GAIN_RE = re.compile(r"AVERAGE POWER GAIN=\s*([-+]?[\d.]+(?:[Ee][-+]?\d+)?)", re.I)


def _to_float(token: str) -> float:
    return float(token.replace("D", "E").replace("d", "e"))


@dataclass
class Impedance:
    tag: int
    segment: int
    voltage_real: float
    voltage_imag: float
    current_real: float
    current_imag: float
    z_real: float
    z_imag: float
    y_real: float
    y_imag: float
    power_w: float

    @property
    def z(self) -> complex:
        return complex(self.z_real, self.z_imag)


@dataclass
class PatternSample:
    theta_deg: float
    phi_deg: float
    vert_db: float
    hor_db: float
    total_db: float
    axial_ratio: float | None = None
    tilt_deg: float | None = None
    sense: str = ""
    e_theta_mag: float | None = None
    e_theta_phase: float | None = None
    e_phi_mag: float | None = None
    e_phi_phase: float | None = None


@dataclass
class FrequencyBlock:
    freq_mhz: float | None
    impedances: list[Impedance] = field(default_factory=list)
    patterns: list[PatternSample] = field(default_factory=list)
    efficiency_percent: float | None = None
    input_power_w: float | None = None
    radiated_power_w: float | None = None
    structure_loss_w: float | None = None
    network_loss_w: float | None = None
    average_power_gain: float | None = None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def max_gain_db(self) -> float | None:
        if not self.patterns:
            return None
        return max(s.total_db for s in self.patterns)

    @property
    def driver(self) -> Impedance | None:
        return self.impedances[0] if self.impedances else None


@dataclass
class NecOutput:
    blocks: list[FrequencyBlock] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    raw: str = ""
    engine_banner: str = ""
    segment_count: int | None = None

    @property
    def ok(self) -> bool:
        return bool(self.blocks) and not self.errors

    @property
    def primary(self) -> FrequencyBlock | None:
        return self.blocks[0] if self.blocks else None


def _section_lines(lines: list[str], start: int) -> list[str]:
    """Return lines of the table that starts after index ``start``."""
    out: list[str] = []
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if not stripped:
            if out:
                break
            continue
        if stripped.startswith("- - -") or stripped.startswith("*****") or stripped.startswith("RUN TIME"):
            break
        out.append(stripped)
    return out


def _parse_impedance_rows(lines: list[str], start: int) -> list[Impedance]:
    rows: list[Impedance] = []
    for line in _section_lines(lines, start):
        tokens = FLOAT_TOKEN.findall(line)
        if len(tokens) < 11:
            continue
        values = [_to_float(t) for t in tokens[:11]]
        rows.append(
            Impedance(
                tag=int(values[0]),
                segment=int(values[1]),
                voltage_real=values[2],
                voltage_imag=values[3],
                current_real=values[4],
                current_imag=values[5],
                z_real=values[6],
                z_imag=values[7],
                y_real=values[8],
                y_imag=values[9],
                power_w=values[10],
            )
        )
    return rows


def _parse_pattern_rows(lines: list[str], start: int) -> list[PatternSample]:
    samples: list[PatternSample] = []
    for line in _section_lines(lines, start):
        tokens = line.split()
        if len(tokens) < 8:
            continue
        try:
            theta = _to_float(tokens[0])
            phi = _to_float(tokens[1])
            vert = _to_float(tokens[2])
            hor = _to_float(tokens[3])
            total = _to_float(tokens[4])
        except ValueError:
            continue
        sample = PatternSample(theta_deg=theta, phi_deg=phi, vert_db=vert, hor_db=hor, total_db=total)
        try:
            sample.axial_ratio = _to_float(tokens[5])
            sample.tilt_deg = _to_float(tokens[6])
            sample.sense = tokens[7]
        except (ValueError, IndexError):
            pass
        if len(tokens) >= 12:
            try:
                sample.e_theta_mag = _to_float(tokens[8])
                sample.e_theta_phase = _to_float(tokens[9])
                sample.e_phi_mag = _to_float(tokens[10])
                sample.e_phi_phase = _to_float(tokens[11])
            except ValueError:
                pass
        samples.append(sample)
    return samples


def parse_output(text: str) -> NecOutput:
    """Parse a complete NEC-2 output text into frequency blocks."""
    result = NecOutput(raw=text)
    lines = text.splitlines()

    banner: list[str] = []
    for line in lines[:60]:
        stripped = line.strip()
        if stripped and "NUMERICAL ELECTROMAGNETICS CODE" not in stripped.upper():
            banner.append(stripped)
        if "COMMENTS" in stripped.upper():
            break
    result.engine_banner = " / ".join(banner[:3])

    # The engine reports the matrix size as MAXMAT= in some builds and as
    # "TOTAL SEGMENTS USED=" in the matrix header of others; accept both.
    seg_match = re.search(r"TOTAL SEGMENTS USED=\s*(\d+)", text)
    if seg_match is None:
        seg_match = re.search(r"MAXMAT=\s*(\d+)", text)
    if seg_match:
        result.segment_count = int(seg_match.group(1))

    freq_positions = [i for i, line in enumerate(lines) if FREQ_RE.search(line)]
    if not freq_positions:
        result.errors, result.warnings = _collect_messages(lines)
        return result

    for idx, start in enumerate(freq_positions):
        end = freq_positions[idx + 1] if idx + 1 < len(freq_positions) else len(lines)
        block_lines = lines[start:end]
        match = FREQ_RE.search(lines[start])
        block = FrequencyBlock(freq_mhz=_to_float(match.group(1)) if match else None)

        for i, line in enumerate(block_lines):
            upper = line.upper()
            if "IMPEDANCE (OHMS)" in upper and not block.impedances:
                block.impedances = _parse_impedance_rows(block_lines, i)
            elif "RADIATION PATTERNS" in upper:
                block.patterns.extend(_parse_pattern_rows(block_lines, i))
            elif "POWER BUDGET" in upper:
                for j in range(i, min(i + 12, len(block_lines))):
                    pm = POWER_RE.search(block_lines[j])
                    if not pm:
                        continue
                    key = pm.group(1).upper()
                    value = _to_float(pm.group(2))
                    if key == "INPUT POWER":
                        block.input_power_w = value
                    elif key == "RADIATED POWER":
                        block.radiated_power_w = value
                    elif key == "STRUCTURE LOSS":
                        block.structure_loss_w = value
                    elif key == "NETWORK LOSS":
                        block.network_loss_w = value
                    elif key == "EFFICIENCY":
                        block.efficiency_percent = value
        for line in block_lines:
            am = AVG_GAIN_RE.search(line)
            if am and block.average_power_gain is None:
                block.average_power_gain = _to_float(am.group(1))
        result.blocks.append(block)

    result.errors, result.warnings = _collect_messages(lines)
    return result


_MESSAGE_RE = re.compile(r"(error|incorrect|fatal|warning|abort|not supported|unrecognized|invalid)", re.I)


def _collect_messages(lines: list[str]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        upper = stripped.upper()
        if upper.startswith("***** DATA CARD"):
            continue
        if "WARNING" in upper:
            warnings.append(stripped)
        elif _MESSAGE_RE.search(stripped):
            errors.append(stripped)
    return list(dict.fromkeys(errors)), list(dict.fromkeys(warnings))
