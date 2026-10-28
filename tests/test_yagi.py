"""Tests for :mod:`nec2core.yagi` and :mod:`nec2core.optimize`.

Run from the project root::

    .venv\\Scripts\\python.exe -m pytest tests/test_yagi.py -q

The engine-backed tests are skipped automatically when the 4NEC2 installation
cannot be located, so the geometry/rendering tests stay usable on any machine.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# ``tests/`` is not a package, so pytest does not put the project root on
# ``sys.path``; add it before importing the package under test.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from nec2core.engine import NecEngine
from nec2core.metrics import summarize_block
from nec2core.model import ModelError, parse_deck
from nec2core.optimize import _set_symbol, apply_variables, optimize_design
from nec2core.output import parse_output
from nec2core.yagi import YagiDesign, YagiElement

FREQ_MHZ = 478.0

# A minimal symbol-parameterised Yagi deck, in the style of 4NEC2's own
# parameterised models: the boom runs along Z, elements are along Y.
SYMBOL_DECK = """CM symbol-parameterised test Yagi
CE
SY Lr=0.320
SY Ld=0.300
SY Hr=Lr/2
SY Hd=Ld/2
SY Zr=0.000
SY Zd1=0.150
SY Zd2=0.300
GW 1 7 0 -Hr Zr 0 Hr Zr 0.004
GW 2 7 0 -Hd Zd1 0 Hd Zd1 0.004
GW 3 7 0 -Hd Zd2 0 Hd Zd2 0.004
GE 0
GN -1
EK
EX 0 2 4 0 1 0
FR 0 0 0 0 478.0 0
RP 0 1 73 1000 90 0 0 5 0 0
RP 0 37 1 1000 0 90 5 0 0 0
EN
"""


# The ``engine`` fixture comes from ``tests/conftest.py`` (session scoped and
# skipped when the 4NEC2 installation is missing).


# ---------------------------------------------------------------------------
# YagiDesign geometry
# ---------------------------------------------------------------------------
def test_classic_geometry_is_self_consistent() -> None:
    design = YagiDesign.classic(FREQ_MHZ, 8, 1.0)
    wavelength = design.wavelength_m

    assert design.element_count == 8
    assert design.boom_length_m == pytest.approx(1.0)
    assert design.validate() == []

    lengths = [element.length_m for element in design.elements]
    positions = [element.position_m for element in design.elements]

    assert lengths[0] == pytest.approx(0.460 * wavelength, rel=1e-4)
    assert lengths[1] == pytest.approx(0.430 * wavelength, rel=1e-4)
    assert lengths[-1] < lengths[2] < lengths[1]
    assert lengths[0] > lengths[1] > lengths[2] > lengths[-1]

    assert positions[0] == 0.0
    assert positions[-1] == pytest.approx(1.0)
    assert positions == sorted(positions)

    description = design.describe()
    assert description["segments"] == 7
    assert len(description["elements"]) == 8
    assert description["elements"][0]["index"] == 1


def test_classic_rejects_single_element() -> None:
    with pytest.raises(ValueError):
        YagiDesign.classic(FREQ_MHZ, 1, 0.5)
    with pytest.raises(ValueError):
        YagiDesign.classic(0.0, 4, 0.5)


def test_classic_default_boom_uses_wavelength() -> None:
    design = YagiDesign.classic(FREQ_MHZ, 4)
    assert design.boom_length_m == pytest.approx(0.2 * 3 * design.wavelength_m, rel=1e-4)


def test_classic_default_boom_is_valid_for_eight_elements() -> None:
    # Mirrors the MCP tool defaults (8 elements, 0.2 lambda per gap): the seven
    # gaps are exactly 0.2 lambda, so validate() must not reject them because
    # of floating-point noise.
    design = YagiDesign.classic(FREQ_MHZ, 8)
    assert design.element_count == 8
    gap = design.elements[1].position_m - design.elements[0].position_m
    assert gap / design.wavelength_m == pytest.approx(0.2, rel=1e-6)
    assert design.validate() == []


# ---------------------------------------------------------------------------
# Deck rendering
# ---------------------------------------------------------------------------
def test_to_nec_renders_a_parseable_deck() -> None:
    design = YagiDesign.classic(FREQ_MHZ, 3, 0.5)
    text = design.to_nec()

    assert text.isascii()
    model = parse_deck(text)
    assert model.errors == []
    assert len(model.wires) == 3
    assert [wire.tag for wire in model.wires] == [1, 2, 3]
    assert model.frequencies_mhz[0] == pytest.approx(FREQ_MHZ)
    assert model.ground["code"] == -1
    assert len(model.rp_cards) == 2
    assert model.excitations and model.excitations[0]["tag"] == 2  # driven element

    # The comment block must come first (NEC-2 requirement) and EN must be last.
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines[0].startswith("CM ")
    assert "CE" in lines[:5]
    assert lines[-1] == "EN"

    # Every element is a straight wire along Y, centred on x = 0, at its boom z.
    for element, wire in zip(design.elements, model.wires):
        assert wire.x1 == pytest.approx(0.0, abs=1e-9)
        assert wire.x2 == pytest.approx(0.0, abs=1e-9)
        assert wire.y1 == pytest.approx(-element.half_length_m, abs=1e-6)
        assert wire.y2 == pytest.approx(element.half_length_m, abs=1e-6)
        assert wire.z1 == pytest.approx(element.position_m, abs=1e-6)
        assert wire.z2 == pytest.approx(element.position_m, abs=1e-6)
        assert wire.segments == design.segments
        assert wire.radius == pytest.approx(design.radius_m, abs=1e-9)


def test_to_nec_perfect_ground_and_feed_position() -> None:
    design = YagiDesign.classic(FREQ_MHZ, 3, 0.4, ground="perfect", feed_element_index=2)
    text = design.to_nec()
    model = parse_deck(text)

    assert model.ground["code"] == 1
    assert "GN 1" in text
    assert model.excitations[0]["tag"] == 2
    assert int(model.excitations[0]["segment"]) == 4  # mid segment of 7


def test_to_nec_rejects_invalid_parameters() -> None:
    good = YagiDesign.classic(FREQ_MHZ, 3, 0.4)
    with pytest.raises(ValueError):
        YagiDesign(**{**good.__dict__, "segments": 2}).to_nec()
    with pytest.raises(ValueError):
        YagiDesign(**{**good.__dict__, "feed_element_index": 0}).to_nec()
    with pytest.raises(ValueError):
        YagiDesign(**{**good.__dict__, "freq_mhz": 0.0}).to_nec()


def test_from_nec_roundtrip() -> None:
    design = YagiDesign.classic(FREQ_MHZ, 6, 0.9)
    rebuilt = YagiDesign.from_nec(design.to_nec())

    assert rebuilt.element_count == design.element_count
    assert rebuilt.freq_mhz == pytest.approx(design.freq_mhz)
    assert rebuilt.segments == design.segments
    assert rebuilt.radius_m == pytest.approx(design.radius_m, rel=1e-6)
    assert rebuilt.feed_element_index == design.feed_element_index
    for original, copy in zip(design.elements, rebuilt.elements):
        # ``to_nec`` writes 5 decimal places, i.e. micrometre resolution.
        assert copy.length_m == pytest.approx(original.length_m, abs=1e-5)
        assert copy.position_m == pytest.approx(original.position_m, abs=1e-5)


def test_from_nec_reads_a_real_4nec2_model(yagi_nec_text: str) -> None:
    design = YagiDesign.from_nec(yagi_nec_text)

    assert design.element_count == 8
    assert design.freq_mhz == pytest.approx(478.875)
    assert design.segments == 5
    assert design.feed_element_index == 2
    assert design.ground == "free"
    assert design.validate() == []

    positions = [element.position_m for element in design.elements]
    assert positions == sorted(positions)
    assert design.elements[-1].length_m < design.elements[0].length_m


def test_from_nec_rejects_non_yagi_decks() -> None:
    with pytest.raises(ModelError):
        YagiDesign.from_nec("CM only one wire\nCE\nGW 1 5 0 0 0 0 0.3 0 0.004\nEN\n")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def test_validate_reports_every_problem() -> None:
    design = YagiDesign(
        freq_mhz=0.0,
        elements=[YagiElement(0.30, 0.20), YagiElement(0.42, 0.05)],
        radius_m=0.0,
        feed_element_index=5,
        segments=4,
        ground="soil",
    )
    text = "\n".join(design.validate())

    assert "freq_mhz must be positive" in text
    assert "radius_m must be positive" in text
    assert "segments must be an odd number" in text
    assert "feed_element_index 5" in text
    assert "ground must be 'free' or 'perfect'" in text
    assert "not beyond element" in text


def test_validate_flags_short_elements_and_tight_spacing() -> None:
    design = YagiDesign(
        freq_mhz=FREQ_MHZ,
        elements=[YagiElement(0.32, 0.0), YagiElement(0.001, 0.005)],
        radius_m=0.001,
    )
    text = "\n".join(design.validate())
    assert "spacing" in text
    assert "at least 2 elements" not in text  # there are two elements

    single = YagiDesign(freq_mhz=FREQ_MHZ, elements=[YagiElement(0.32, 0.0)])
    assert "at least 2 elements" in "\n".join(single.validate())


# ---------------------------------------------------------------------------
# Engine-backed tests
# ---------------------------------------------------------------------------
@pytest.mark.engine
def test_classic_design_runs_in_the_engine(engine: NecEngine) -> None:
    design = YagiDesign.classic(FREQ_MHZ, 8, 1.0)
    result = engine.run(design.to_nec())

    assert result.ok, f"engine rejected the deck: {result.errors}\n{result.output[-2000:]}"
    assert parse_deck(result.deck).errors == []

    blocks = parse_output(result.output).blocks
    assert blocks, "no frequency block in the engine output"
    metrics = summarize_block(blocks[0], 50.0)
    assert metrics["gain_max_db"] is not None
    assert metrics["gain_max_db"] > 0.0
    assert metrics["swr"] is not None


@pytest.mark.engine
def test_default_eight_element_design_is_a_usable_antenna(engine: NecEngine) -> None:
    """The default geometry must stay a real Yagi, not just a deck that runs.

    Over-long elements keep passing ``validate()`` and still simulate, but the
    array degenerates to ~5 dBi with a badly mistuned feed; feeding the
    reflector instead of the driven element (``feed_element_index == 1``) also
    costs about 1.5 dB of forward gain.  This pins the tuned taper, the driven
    element feed point and a usable match.
    """
    design = YagiDesign.classic(FREQ_MHZ, 8)
    assert design.feed_element_index == 2
    assert design.feed_element.length_m < design.elements[0].length_m  # driven < reflector
    result = engine.run(design.to_nec())

    assert result.ok, f"engine rejected the deck: {result.errors}\n{result.output[-2000:]}"
    metrics = summarize_block(parse_output(result.output).blocks[0], 50.0)
    assert metrics["gain_max_db"] > 11.0
    assert metrics["swr"] < 3.0
    assert metrics["fb_db"] > 10.0


# ---------------------------------------------------------------------------
# Optimiser: deck rendering (no engine needed)
# ---------------------------------------------------------------------------
def test_apply_variables_rewrites_a_symbol() -> None:
    text = apply_variables(SYMBOL_DECK, {"Ld": 0.31}, [{"symbol": "Ld", "min": 0.28, "max": 0.32}])

    # NEC-2 has no SY card, so the renderer substitutes the numeric value.
    assert not any(line.upper().startswith("SY") for line in text.splitlines())
    model = parse_deck(text)
    assert model.errors == []
    assert model.symbols == {}
    # Ld drives both directors (GW 2 and GW 3), the reflector uses Lr.
    assert model.wires[1].length_m == pytest.approx(0.31, abs=1e-6)
    assert model.wires[2].length_m == pytest.approx(0.31, abs=1e-6)
    assert model.wires[0].length_m == pytest.approx(0.320, abs=1e-6)
    # The comment block survives the expansion.
    assert text.startswith("CM ")


def test_set_symbol_keeps_inline_comments() -> None:
    deck = SYMBOL_DECK.replace("SY Ld=0.300", "SY Ld=0.300 ' director length")
    text = _set_symbol(deck, "Ld", 0.295)

    assert "SY Ld=0.295 ' director length" in text
    # The other symbols are untouched.
    assert "SY Lr=0.320" in text


def test_apply_variables_inserts_a_missing_symbol() -> None:
    plain = YagiDesign.classic(FREQ_MHZ, 3, 0.4).to_nec()
    text = apply_variables(plain, {"Ld": 0.3}, [{"symbol": "Ld", "min": 0.28, "max": 0.32}])

    assert not any(line.upper().startswith("SY") for line in text.splitlines())
    parsed = parse_deck(text)
    assert parsed.errors == []
    assert len(parsed.wires) == 3
    assert parsed.wires[0].length_m == pytest.approx(parse_deck(plain).wires[0].length_m, abs=1e-6)


def test_apply_variables_scales_an_element_length() -> None:
    design = YagiDesign.classic(FREQ_MHZ, 4, 0.6)
    original = design.to_nec()
    before = parse_deck(original).wires[2].length_m

    text = apply_variables(
        original,
        {"dir2": before + 0.01},
        [{"kind": "element_length", "index": 3, "name": "dir2", "min": before - 0.01, "max": before + 0.02}],
    )
    model = parse_deck(text)
    assert model.errors == []
    assert model.wires[2].length_m == pytest.approx(before + 0.01, abs=1e-6)
    # Neighbouring elements and the boom positions must be untouched.
    assert model.wires[1].length_m == pytest.approx(parse_deck(original).wires[1].length_m, abs=1e-6)
    assert model.wires[2].center[2] == pytest.approx(parse_deck(original).wires[2].center[2], abs=1e-6)


def test_apply_variables_moves_an_element() -> None:
    original = YagiDesign.classic(FREQ_MHZ, 4, 0.6).to_nec()
    model = parse_deck(original)
    current = model.wires[3].center[2]

    text = apply_variables(
        original,
        {"dir3": current + 0.05},
        [{"kind": "element_position", "index": 4, "name": "dir3", "min": current, "max": current + 0.1}],
    )
    moved = parse_deck(text)
    assert moved.errors == []
    assert moved.wires[3].center[2] == pytest.approx(current + 0.05, abs=1e-6)
    assert moved.wires[3].length_m == pytest.approx(model.wires[3].length_m, abs=1e-6)


def test_apply_variables_rebuilds_a_yagi_design() -> None:
    original = YagiDesign.classic(FREQ_MHZ, 4, 0.6).to_nec()
    text = apply_variables(
        original,
        {"radius": 0.005, "len5": 0.25},
        [
            {"kind": "yagi", "field": "radius", "name": "radius", "min": 0.003, "max": 0.006},
            {"kind": "yagi", "field": "length:3", "name": "len5", "min": 0.2, "max": 0.3},
        ],
    )
    model = parse_deck(text)
    assert model.errors == []
    assert model.wires[0].radius == pytest.approx(0.005, abs=1e-9)
    assert model.wires[2].length_m == pytest.approx(0.25, abs=1e-9)


# ---------------------------------------------------------------------------
# Optimiser: argument handling (no engine needed)
# ---------------------------------------------------------------------------
class _NeverEngine:
    """Stand-in engine that fails loudly if the optimiser ever calls it."""

    def run(self, *args, **kwargs):  # noqa: ANN001 - test double
        raise AssertionError("engine must not be called for these arguments")


def test_optimize_design_rejects_unknown_objective() -> None:
    with pytest.raises(ValueError):
        optimize_design(
            SYMBOL_DECK,
            [{"symbol": "Ld", "min": 0.28, "max": 0.32}],
            objective="directivity",
            engine=_NeverEngine(),
        )


def test_optimize_design_rejects_bad_arguments() -> None:
    variables = [{"symbol": "Ld", "min": 0.28, "max": 0.32}]
    with pytest.raises(ValueError):
        optimize_design(SYMBOL_DECK, variables, max_evals=0, engine=_NeverEngine())
    with pytest.raises(ValueError):
        optimize_design(SYMBOL_DECK, [], engine=_NeverEngine())
    with pytest.raises(ValueError):
        optimize_design("CM empty\nCE\nEN\n", variables, engine=_NeverEngine())


# ---------------------------------------------------------------------------
# Optimiser: end-to-end against the engine
# ---------------------------------------------------------------------------
@pytest.mark.engine
def test_optimize_design_tunes_a_symbol(engine: NecEngine) -> None:
    result = optimize_design(
        SYMBOL_DECK,
        [{"symbol": "Ld", "min": 0.28, "max": 0.32, "step": 0.01}],
        objective="gain",
        engine=engine,
        max_evals=5,
        z0=50.0,
        timeout_s=120.0,
    )

    assert set(result) >= {"best", "score", "metrics", "evaluations", "history"}
    assert 1 <= result["evaluations"] <= 5
    assert result["best"]["Ld"] == pytest.approx(0.30, abs=0.05)
    assert "error" not in result["metrics"], result["metrics"]
    assert result["history"] and result["history"][0]["accepted"] is True
    assert all(entry["eval"] <= 5 for entry in result["history"])

    # The reported best deck is a valid, self-contained deck.
    assert parse_deck(result["best_deck"]).errors == []

    dump = json.dumps(result)
    assert "NaN" not in dump and "Infinity" not in dump


@pytest.mark.engine
def test_optimize_design_tunes_an_element_length(engine: NecEngine) -> None:
    deck = YagiDesign.classic(FREQ_MHZ, 3, 0.5).to_nec()
    before = parse_deck(deck).wires[1].length_m

    result = optimize_design(
        deck,
        [{"kind": "element_length", "index": 2, "name": "driven", "min": before - 0.01, "max": before + 0.01, "step": 0.005}],
        objective="swr",
        engine=engine,
        max_evals=3,
    )

    assert 1 <= result["evaluations"] <= 3
    assert result["best"]["driven"] == pytest.approx(before, abs=0.02)
    assert "error" not in result["metrics"], result["metrics"]
    tuned = parse_deck(result["best_deck"])
    assert tuned.errors == []
    assert tuned.wires[1].length_m == pytest.approx(result["best"]["driven"], abs=1e-6)
