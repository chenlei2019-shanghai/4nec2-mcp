"""Tests for ``nec2core.engine`` against the real 4NEC2 installation."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from nec2core.engine import NecEngine, list_engines, preferred_engine_path, sanitize_deck
from nec2core.output import parse_output

pytestmark = pytest.mark.engine


def test_list_engines_finds_the_real_installation(nec4nec_dir):
    engines = list_engines(nec4nec_dir)
    assert engines, "no NEC-2 engines discovered"
    preferred = [engine for engine in engines if engine["preferred"]]
    assert len(preferred) == 1
    assert preferred[0]["name"].lower() == "nec2dxs11k.exe"
    assert preferred[0]["path"] == str(preferred_engine_path(nec4nec_dir))
    for engine in engines:
        assert engine["size_bytes"] > 0
        assert engine["folder"]


def test_engine_runs_yagi_deck(engine, yagi_nec_text):
    result = engine.run(yagi_nec_text, timeout_s=30)
    assert result.ok is True, result.errors
    assert result.exit_code == 0
    assert "FREQUENCY=" in result.output
    assert result.engine_name.lower() == "nec2dxs11k.exe"
    assert result.duration_s < 30.0
    parsed = parse_output(result.output)
    assert len(parsed.blocks) == 1
    assert parsed.blocks[0].driver is not None


def test_engine_run_respects_frequency_override(engine, yagi_nec_text):
    # The fixture's FR card is column formatted (several spaces between fields).
    deck = re.sub(r"(?m)^FR\s+.*$", "FR 0 0 0 0 490.0 0", yagi_nec_text)
    assert "490.0" in deck
    result = engine.run(deck, timeout_s=30)
    assert result.ok is True, result.errors
    parsed = parse_output(result.output)
    assert parsed.blocks[0].freq_mhz == pytest.approx(490.0, abs=0.01)


def test_save_out_path_is_byte_exact(engine, yagi_nec_text, tmp_path):
    """Regression: text-mode writing turned the engine's CRLF into CRCRLF."""
    target = tmp_path / "yagi.out"
    result = engine.run(yagi_nec_text, timeout_s=30, save_out_path=target)
    assert result.saved_out_path == str(target)
    data = target.read_bytes()
    assert b"\r\r\n" not in data
    assert b"\r\n" in data  # the engine really emits CRLF
    parsed = parse_output(data.decode("ascii", "replace"))
    assert len(parsed.blocks[0].patterns) >= 100


def test_sanitize_deck_keeps_comments_and_appends_en():
    deck = "CM folded dipole comment\nCE\nGW 1 1 0 0 0 0 1 0 0.001\nFR 0 0 0 0 100 0\n"
    sanitized = sanitize_deck(deck)
    assert sanitized.startswith("CM folded dipole comment")
    assert "CE" in sanitized
    assert sanitized.splitlines()[-1] == "EN"
    assert sum(1 for line in sanitized.splitlines() if line[:2].upper() == "EN") == 1


def test_sanitize_deck_does_not_duplicate_en():
    deck = "CM comment\nGW 1 1 0 0 0 0 1 0 0.001\nEN\n"
    sanitized = sanitize_deck(deck)
    assert sum(1 for line in sanitized.splitlines() if line[:2].upper() == "EN") == 1
    assert "CM comment" in sanitized


def test_sanitize_deck_replaces_non_ascii_on_comment_cards():
    sanitized = sanitize_deck("CM 中文注释\nEN\n")
    assert "中文" not in sanitized
    assert sanitized.startswith("CM ??")


def test_engine_rejects_unknown_engine_name(nec4nec_dir):
    from nec2core.engine import NecError, resolve_engine

    with pytest.raises(NecError):
        resolve_engine("no-such-engine.exe", nec4nec_dir)


def test_custom_engine_selection(nec4nec_dir, yagi_nec_text):
    engine = NecEngine(nec4nec_dir, engine="nec2dxs11k.exe", timeout_s=30)
    assert Path(engine.engine_path).name.lower() == "nec2dxs11k.exe"
    result = engine.run(yagi_nec_text, timeout_s=30)
    assert result.ok is True, result.errors
