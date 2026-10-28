"""Fixtures shared by the 4NEC2 test suite.

The decks under ``tests/fixtures`` are copies of real models shipped with 4NEC2
and ``478MHz_8El_Yagi.out`` was produced by the real engine (nec2dxs11k.exe), so
the parser/metrics tests run offline while staying faithful to production output.
Engine and MCP-server tests are marked ``engine``/``server`` in ``pytest.ini``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nec2core.engine import NecEngine, find_nec4nec_dir
from nec2core.output import NecOutput, parse_output

TESTS_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = TESTS_DIR / "fixtures"
YAGI_NEC_PATH = FIXTURES_DIR / "478MHz_8El_Yagi.nec"
FOLDED_NEC_PATH = FIXTURES_DIR / "470-500MHz_8el_folded_200ohm_optimized_candidate.nec"
YAGI_OUT_PATH = FIXTURES_DIR / "478MHz_8El_Yagi.out"
SERVER_SCRIPT = TESTS_DIR.parent / "mcp_4nec2_server.py"

# Real installation; can be overridden for CI machines with a different layout.
NEC4NEC_DIR = Path(os.environ.get("NEC4NEC_DIR", r"C:\4nec2"))


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


@pytest.fixture(scope="session")
def nec4nec_dir() -> Path:
    """The 4NEC2 installation used by engine/server tests (skips when absent)."""
    if not (NEC4NEC_DIR / "exe").is_dir():
        pytest.skip(f"4NEC2 installation not found at {NEC4NEC_DIR}")
    return find_nec4nec_dir(NEC4NEC_DIR)


@pytest.fixture(scope="session")
def engine(nec4nec_dir: Path) -> NecEngine:
    return NecEngine(nec4nec_dir, timeout_s=30.0)


@pytest.fixture(scope="session")
def yagi_nec_text() -> str:
    return _read(YAGI_NEC_PATH)


@pytest.fixture(scope="session")
def folded_nec_text() -> str:
    return _read(FOLDED_NEC_PATH)


@pytest.fixture(scope="session")
def yagi_out_text() -> str:
    return _read(YAGI_OUT_PATH)


@pytest.fixture(scope="session")
def parsed_yagi_out(yagi_out_text: str) -> NecOutput:
    parsed = parse_output(yagi_out_text)
    assert parsed.blocks, parsed.errors
    return parsed


@pytest.fixture(scope="session")
def yagi_block(parsed_yagi_out: NecOutput):
    return parsed_yagi_out.blocks[0]
