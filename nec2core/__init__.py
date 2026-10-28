"""Core library for the 4NEC2 MCP server (engine, parser, metrics, models)."""

from .engine import (
    NecEngine,
    NecError,
    RunResult,
    find_nec4nec_dir,
    list_engines,
    preferred_engine_path,
    sanitize_deck,
)
from .model import ModelError, describe_model, parse_deck, set_frequency, set_single_frequency
from .metrics import DEFAULT_Z0, best_frequency, peak_sample, swr, summarize_block
from .output import FrequencyBlock, Impedance, NecOutput, PatternSample, parse_output

__all__ = [
    "NecEngine",
    "NecError",
    "RunResult",
    "find_nec4nec_dir",
    "list_engines",
    "preferred_engine_path",
    "sanitize_deck",
    "ModelError",
    "parse_deck",
    "describe_model",
    "set_frequency",
    "set_single_frequency",
    "DEFAULT_Z0",
    "swr",
    "summarize_block",
    "best_frequency",
    "peak_sample",
    "NecOutput",
    "FrequencyBlock",
    "Impedance",
    "PatternSample",
    "parse_output",
]
