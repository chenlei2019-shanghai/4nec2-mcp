"""Tests for ``nec2core.model``: card parsing, SY symbols, expressions."""

from __future__ import annotations

import pytest

from nec2core.model import (
    ModelError,
    describe_model,
    eval_expr,
    parse_deck,
    set_single_frequency,
)


def test_yagi_deck_card_structure(yagi_nec_text):
    model = parse_deck(yagi_nec_text)
    assert model.errors == []
    assert model.warnings == []
    assert len(model.wires) == 8
    assert model.total_segments == 40
    assert model.ground == {"code": -1, "type": "free space"}
    assert model.frequencies_mhz == pytest.approx([478.875])
    assert len(model.excitations) == 1
    excitation = model.excitations[0]
    assert excitation["type"] == "voltage"
    assert excitation["tag"] == 2
    assert excitation["segment"] == "3"
    assert excitation["voltage"] == pytest.approx(1.0)


def test_yagi_deck_geometry(yagi_nec_text):
    model = parse_deck(yagi_nec_text)
    # All 8 elements are vertical wires along +Y, stepped along +Z.
    assert [w.tag for w in model.wires] == list(range(1, 9))
    assert all(w.orientation() == "Y" for w in model.wires)
    assert all(w.segments == 5 for w in model.wires)
    first, last = model.wires[0], model.wires[-1]
    assert first.length_m == pytest.approx(0.298, abs=1e-6)
    assert last.length_m == pytest.approx(0.2376, abs=1e-6)
    assert last.z1 > first.z1
    box = model.bounding_box()
    assert box["dx_m"] == pytest.approx(0.0)
    assert box["dy_m"] == pytest.approx(0.298, abs=1e-3)
    assert box["dz_m"] == pytest.approx(1.1495, abs=1e-3)


def test_folded_yagi_symbols_are_substituted(folded_nec_text):
    model = parse_deck(folded_nec_text)
    assert model.errors == []
    assert model.warnings == []
    assert len(model.wires) == 11
    assert model.total_segments == 197

    # SY symbols are evaluated (and looked up case-insensitively).
    assert model.symbols["LR"] == pytest.approx(0.322, abs=1e-9)
    assert model.symbols["HR"] == pytest.approx(0.161, abs=1e-9)
    assert model.symbols["LFD"] == pytest.approx(0.275, abs=1e-9)
    assert model.symbols["ZFRONT"] == pytest.approx(-0.0025, abs=1e-9)
    assert model.symbols["ZBACK"] == pytest.approx(0.0525, abs=1e-9)

    # The folded driver (tag 1) runs from -Hr to +Hr at Zel.
    wire = model.wires[0]
    assert (wire.tag, wire.segments) == (1, 21)
    assert wire.x1 == pytest.approx(0.0)
    assert wire.y1 == pytest.approx(-0.161)
    assert wire.y2 == pytest.approx(0.161)
    assert wire.z1 == pytest.approx(0.025)
    assert wire.radius == pytest.approx(0.004)

    # The folded dipole's second half (tag 2) is at Xde = 0.145, Zfront = -0.0025.
    paras = model.wires[1]
    assert (paras.tag, paras.segments) == (2, 22)
    assert paras.x1 == pytest.approx(0.145)
    assert paras.z1 == pytest.approx(-0.0025)

    # The shorting stub (tag 4) spans the fold gap Zfront..Zback.
    stub = model.wires[3]
    assert (stub.tag, stub.segments) == (4, 3)
    assert stub.z1 == pytest.approx(-0.0025)
    assert stub.z2 == pytest.approx(0.0525)


def test_folded_yagi_sweep_and_describe(folded_nec_text):
    model = parse_deck(folded_nec_text)
    assert len(model.frequencies_mhz) == 61
    assert model.frequencies_mhz[0] == pytest.approx(460.0)
    assert model.frequencies_mhz[-1] == pytest.approx(520.0)
    assert model.frequencies_mhz[1] - model.frequencies_mhz[0] == pytest.approx(1.0)

    description = describe_model(model)
    assert description["total_segments"] == 197
    assert [element["tag"] for element in description["elements"]] == list(range(1, 12))
    assert description["symbols"]["LR"] == pytest.approx(0.322)
    assert description["ground"] == {"code": -1, "type": "free space"}
    assert description["radiation_patterns"][0]["n_theta"] == 37
    assert description["radiation_patterns"][0]["n_phi"] == 73


def test_set_single_frequency_rewrites_fr_card(folded_nec_text):
    edited = set_single_frequency(folded_nec_text, 490.0)
    model = parse_deck(edited)
    assert model.errors == []
    assert model.frequencies_mhz == pytest.approx([490.0])
    assert len(model.wires) == 11  # geometry untouched


def test_eval_expr_accepts_case_insensitive_symbols():
    # Regression: ``SY Lr=0.322`` stores "LR", but decks reference "Lr/2".
    assert eval_expr("LR/2", {"LR": 0.322}) == pytest.approx(0.161)
    assert eval_expr("Lr/2", {"LR": 0.322}) == pytest.approx(0.161)
    assert eval_expr("sin(30)") == pytest.approx(0.5, abs=1e-9)
    assert eval_expr("sqrt(4)") == pytest.approx(2.0)


def test_eval_expr_rejects_unsafe_expressions():
    for expression in ("__import__('os')", "open('x')", "(lambda: 1)()"):
        with pytest.raises(ModelError):
            eval_expr(expression)
