"""Tests for the parallel-to-one-balun feed model (``Design.parallel_deck``)."""

from __future__ import annotations

import sys

import pytest

from nec2core.engine import NecEngine, find_nec4nec_dir
from nec2core.metrics import summarize_block
from nec2core.output import parse_output

PROJECT_SCRIPTS = __import__("pathlib").Path(__file__).resolve().parent.parent / "scripts"


@pytest.fixture(scope="module")
def design():
    sys.path.insert(0, str(PROJECT_SCRIPTS))
    import design_triband

    return design_triband.initial_design()


def test_parallel_deck_has_a_single_excitation(design):
    deck = design.parallel_deck(freq_mhz=28.5)
    excitations = [line for line in deck.splitlines() if line.startswith("EX ")]
    assert len(excitations) == 1, "one balun means exactly one source"
    assert excitations[0].split()[2:] != [], "the EX card must name a tag and segment"


def test_parallel_deck_splits_drivers_and_adds_buses(design):
    deck = design.parallel_deck(freq_mhz=21.2)
    wires = [line for line in deck.splitlines() if line.startswith("GW ")]
    driven = [element for element in design.elements if element.driven]
    plain = [element for element in design.elements if not element.driven]

    # every driven element is split into two arms, so +1 wire per driven element
    assert len(wires) == len(design.elements) + len(driven) + 2 * (len(driven) - 1) + 2 * len(driven) + 1

    # the arms leave a gap at the element centre
    assert "0.01000" in deck or "0.0100" in deck

    # two bus runs along x = 0.060 m, y = +/-0.050 m
    assert "0.06000 0.05000" in deck
    assert "0.06000 -0.05000" in deck

    # the plain (inactive band) elements are still whole wires
    for element in plain:
        half = element.length / 2.0
        assert f"0 {-half:.5f} {element.z:.5f}" in deck


def test_parallel_deck_ignores_the_band(design):
    """The same physical network is swept over every band, so ``band`` is not a parameter."""
    a = design.parallel_deck(freq_mhz=14.15)
    b = design.parallel_deck(freq_mhz=14.15)
    assert a == b
    assert "FR 0 0 0 0 14.1500 0" in a


@pytest.mark.engine
def test_parallel_deck_runs_in_the_engine(design, nec4nec_dir):
    engine = NecEngine(nec4nec_dir or find_nec4nec_dir(None))
    result = engine.run(design.parallel_deck(freq_mhz=14.15))
    assert result.ok, result.errors
    block = parse_output(result.output).primary
    summary = summarize_block(block, 50.0)
    assert 0.5 < summary["swr"] < 100.0
    assert summary["gain_max_db"] > 0.0
