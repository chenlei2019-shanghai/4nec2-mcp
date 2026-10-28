"""Tests for ``nec2core.output`` against real NEC-2 engine output.

``tests/fixtures/478MHz_8El_Yagi.out`` is the verbatim output of
``nec2dxs11k.exe`` for ``tests/fixtures/478MHz_8El_Yagi.nec`` measured at
478.875 MHz with a 50 ohm feed.
"""

from __future__ import annotations

import pytest

from nec2core.output import parse_output

# Columns: TAG SEG. V R I V I Z REAL Z IMAG Y REAL Y IMAG POWER.
# The impedance pair is deliberately glued (no space before the negative sign),
# exactly as NEC-2 prints it: "1.92595E+01-1.35152E+01".
GLUED_IMPEDANCE_OUTPUT = """\
                             NUMERICAL ELECTROMAGNETICS CODE
FREQUENCY= 1.9000E+01 MHZ
                    --------- IMPEDANCE (OHMS) ---------
   TAG   SEG.   VOLTAGE (VOLTS)   CURRENT (AMPS)   IMPEDANCE (OHMS)   ADMITTANCE (MHOS)   POWER
   NO.   NO.    REAL      IMAG     REAL     IMAG    REAL      IMAG     REAL      IMAG    (WATTS)
     1     1 1.00000E+00 0.00000E+00 2.00000E-02 0.00000E+00 1.92595E+01-1.35152E+01 2.00000E-02-1.00000E-02 1.00000E-02
"""

# A radiation-pattern table mixing a null marker (-999.99) with a real gain.
NULL_GAIN_OUTPUT = """\
                             NUMERICAL ELECTROMAGNETICS CODE
FREQUENCY= 1.0000E+02 MHZ
 - - - RADIATION PATTERNS - - -
  THETA     PHI       VERT.   HOR.    TOTAL   AXIAL   TILT    SENSE
   90.00    0.00    -999.99 -999.99 -999.99    0.00    0.00  LINEAR
   90.00    5.00     -12.77  -12.77  -12.77    0.00    0.00  LINEAR
"""


def test_fixture_parses_one_frequency_block(parsed_yagi_out):
    assert len(parsed_yagi_out.blocks) == 1
    block = parsed_yagi_out.blocks[0]
    # The engine prints 4.7888E+02 (4 significant digits), so compare to 0.01 MHz.
    assert block.freq_mhz == pytest.approx(478.875, abs=0.01)
    assert parsed_yagi_out.errors == []
    assert parsed_yagi_out.ok is True


def test_fixture_driver_impedance(parsed_yagi_out):
    driver = parsed_yagi_out.blocks[0].driver
    assert driver is not None
    # NEC numbers segments absolutely in its output tables: the deck feeds
    # tag 2 / segment 3 (tag relative), i.e. segment 8 of 40 overall.
    assert (driver.tag, driver.segment) == (2, 8)
    assert driver.z_real == pytest.approx(17.227, abs=0.01)
    assert driver.z_imag == pytest.approx(-19.203, abs=0.01)
    assert driver.z == pytest.approx(complex(17.227, -19.203), abs=0.01)


def test_fixture_radiation_patterns(parsed_yagi_out):
    block = parsed_yagi_out.blocks[0]
    assert len(block.patterns) >= 100
    assert block.max_gain_db == pytest.approx(13.47, abs=0.01)
    peak = max(block.patterns, key=lambda s: s.total_db)
    assert (peak.theta_deg, peak.phi_deg) == pytest.approx((0.0, 90.0))
    assert peak.vert_db > -999.0  # vertical polarization is populated
    for sample in block.patterns:
        assert 0.0 <= sample.theta_deg <= 180.0
        assert 0.0 <= sample.phi_deg <= 360.0


def test_fixture_power_budget(parsed_yagi_out):
    block = parsed_yagi_out.blocks[0]
    assert block.efficiency_percent == pytest.approx(100.0, abs=0.5)
    assert block.input_power_w == pytest.approx(0.012943, rel=1e-3)
    assert block.radiated_power_w == pytest.approx(0.012943, rel=1e-3)
    # The engine reports the matrix size as TOTAL SEGMENTS USED= 40 (there
    # is no MAXMAT line in its output); the parser must pick it up.
    assert parsed_yagi_out.segment_count == 40
    assert block.average_power_gain is None


def test_glued_negative_impedance_is_parsed():
    parsed = parse_output(GLUED_IMPEDANCE_OUTPUT)
    assert parsed.blocks, parsed.errors
    driver = parsed.blocks[0].driver
    assert driver is not None
    assert (driver.tag, driver.segment) == (1, 1)
    assert driver.z_real == pytest.approx(19.2595, abs=1e-6)
    assert driver.z_imag == pytest.approx(-13.5152, abs=1e-6)
    # 1.92595E+01-1.35152E+01 must not be read as a single 19.2595e1 token.
    assert driver.z_imag < 0


def test_null_gain_marker_is_kept_and_excluded_from_max_gain():
    parsed = parse_output(NULL_GAIN_OUTPUT)
    block = parsed.blocks[0]
    assert len(block.patterns) == 2
    null_sample, real_sample = block.patterns
    assert null_sample.total_db == pytest.approx(-999.99)
    assert real_sample.total_db == pytest.approx(-12.77)
    # -999.99 is the "no gain" marker; max_gain_db must not be the marker.
    assert block.max_gain_db == pytest.approx(-12.77)
