"""End-to-end tests that drive ``mcp_4nec2_server.py`` over stdio, like DSH does."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

pytestmark = pytest.mark.server

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "mcp_4nec2_server.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

YAGI_NEC = FIXTURES / "478MHz_8El_Yagi.nec"
YAGI_OUT = FIXTURES / "478MHz_8El_Yagi.out"

EXPECTED_TOOLS = {
    "nec_list_engines",
    "nec_list_models",
    "nec_describe",
    "nec_run",
    "nec_sweep",
    "nec_parse_out",
    "nec_build_yagi",
    "nec_optimize",
    "nec_plot_pattern",
    "nec_plot_sweep",
    "nec_open_gui",
}


def _params(nec4nec_dir: Path) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER), "--4nec2-dir", str(nec4nec_dir)],
        cwd=str(ROOT),
    )


def _payload(result) -> dict:
    assert result.isError is False, result
    text = "".join(getattr(item, "text", "") for item in result.content)
    return json.loads(text)


def _run(coro):
    if sys.platform == "win32":
        # The stdio client needs a subprocess-capable event loop on Windows.
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    return asyncio.run(coro)


async def _with_session(scenario, nec4nec_dir: Path):
    async with stdio_client(_params(nec4nec_dir)) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            return await scenario(session, init)


def test_server_exposes_all_nec_tools(nec4nec_dir):
    async def scenario(session: ClientSession, init):
        assert init.serverInfo.name == "4nec2"
        tools = await session.list_tools()
        return {tool.name for tool in tools.tools}

    names = _run(_with_session(scenario, nec4nec_dir))
    assert EXPECTED_TOOLS <= names


def test_nec_run_executes_the_fixture_model(nec4nec_dir):
    async def scenario(session: ClientSession, init):
        result = await session.call_tool("nec_run", {"path": str(YAGI_NEC)})
        return _payload(result)

    payload = _run(_with_session(scenario, nec4nec_dir))
    assert payload["status"] == "ok", payload
    assert payload["freq_mhz"] == pytest.approx(478.875, abs=0.01)
    assert abs(payload["z_real"] - 17.227) < 0.1
    assert payload["z_imag"] == pytest.approx(-19.203, abs=0.1)
    assert payload["swr"] == pytest.approx(3.379, abs=0.02)
    assert payload["gain_max_db"] == pytest.approx(13.47, abs=0.05)
    assert payload["fb_db"] > 20.0
    assert payload["segments"] == 40  # TOTAL SEGMENTS USED= 40
    assert payload["status"] == "ok"


def test_nec_run_can_return_pattern_samples(nec4nec_dir):
    async def scenario(session: ClientSession, init):
        result = await session.call_tool(
            "nec_run", {"path": str(YAGI_NEC), "include_pattern": True}
        )
        return _payload(result)

    payload = _run(_with_session(scenario, nec4nec_dir))
    assert payload["status"] == "ok"
    assert len(payload["pattern"]) >= 100
    peak = max(payload["pattern"], key=lambda sample: sample["total_db"])
    assert peak["total_db"] == pytest.approx(13.47, abs=0.05)


def test_describe_list_engines_and_parse_out(nec4nec_dir):
    async def scenario(session: ClientSession, init):
        engines = _payload(await session.call_tool("nec_list_engines", {}))
        models = _payload(await session.call_tool("nec_list_models", {"directory": str(nec4nec_dir / "models")}))
        described = _payload(await session.call_tool("nec_describe", {"path": str(YAGI_NEC)}))
        parsed = _payload(await session.call_tool("nec_parse_out", {"path": str(YAGI_OUT)}))
        return engines, models, described, parsed

    engines, models, described, parsed = _run(_with_session(scenario, nec4nec_dir))

    assert engines["status"] == "ok"
    assert engines["preferred"].lower().endswith("nec2dxs11k.exe")
    assert engines["engines"]

    assert models["status"] == "ok"
    assert models["count"] >= 1

    assert described["status"] == "ok", described
    assert described["total_segments"] == 40
    assert len(described["wires"]) == 8
    assert described["frequency_mhz"] == pytest.approx([478.875])
    assert described["ground"] == {"code": -1, "type": "free space"}

    assert parsed["status"] == "ok", parsed
    assert len(parsed["blocks"]) == 1
    block = parsed["blocks"][0]
    assert block["z_real"] == pytest.approx(17.227, abs=0.01)
    assert "fb_db" in block


def test_nec_sweep_returns_one_row_per_frequency(nec4nec_dir):
    async def scenario(session: ClientSession, init):
        result = await session.call_tool(
            "nec_sweep",
            {"path": str(YAGI_NEC), "start_mhz": 470, "stop_mhz": 480, "step_mhz": 5},
        )
        return _payload(result)

    payload = _run(_with_session(scenario, nec4nec_dir))
    assert payload["status"] == "ok", payload
    assert payload["points"] == 3
    assert len(payload["rows"]) == 3
    assert [row["freq_mhz"] for row in payload["rows"]] == pytest.approx([470.0, 475.0, 480.0])
    assert payload["best_swr"]["swr"] <= min(row["swr"] for row in payload["rows"])
    # Regression: multi-frequency runs must not lose the elevation cut
    # (before the fix 470/475 MHz reported -14.38/-12.81 dB).
    assert [round(row["gain_max_db"], 2) for row in payload["rows"]] == pytest.approx(
        [12.92, 13.26, 13.51], abs=0.05
    ), payload["rows"]
    assert [round(row["fb_db"], 2) for row in payload["rows"]] == pytest.approx(
        [17.47, 19.64, 23.88], abs=0.1
    ), payload["rows"]


def test_nec_plot_pattern_renders_over_stdio(nec4nec_dir, tmp_path):
    """Regression: the first numpy/matplotlib import used to deadlock.

    Importing numpy's C extension for the first time inside an anyio worker
    thread blocked forever, so ``nec_plot_pattern`` never answered and the
    client timed out.  The server now primes matplotlib from its main thread
    (``_preload_plotting``); this test drives the real stdio transport to prove
    the call returns.
    """

    async def scenario(session: ClientSession, init):
        result = await session.call_tool(
            "nec_plot_pattern",
            {"path": str(YAGI_NEC), "out_dir": str(tmp_path), "include_3d": True},
        )
        return _payload(result)

    payload = _run(_with_session(scenario, nec4nec_dir))
    assert payload["status"] == "ok", payload
    assert {figure["kind"] for figure in payload["figures"]} == {"polar", "3d"}
    for figure in payload["figures"]:
        assert Path(figure["path"]).stat().st_size > 20_000


def test_invalid_arguments_return_status_error(nec4nec_dir):
    async def scenario(session: ClientSession, init):
        empty_run = _payload(await session.call_tool("nec_run", {}))
        missing_file = _payload(
            await session.call_tool("nec_describe", {"path": "definitely-not-a-model.nec"})
        )
        no_variables = _payload(
            await session.call_tool("nec_optimize", {"path": str(YAGI_NEC)})
        )
        bad_geometry = _payload(
            await session.call_tool(
                "nec_build_yagi",
                {
                    "freq_mhz": 478.0,
                    "element_lengths_m": [0.3, 0.3],
                    "element_positions_m": [0.0],
                },
            )
        )
        return empty_run, missing_file, no_variables, bad_geometry

    empty_run, missing_file, no_variables, bad_geometry = _run(_with_session(scenario, nec4nec_dir))

    assert empty_run["status"] == "error"
    assert "path" in empty_run["error"]

    assert missing_file["status"] == "error"
    assert "not found" in missing_file["error"]

    # nec_build_yagi / nec_optimize depend on optional modules; only the
    # registration and the error contract are asserted here.
    assert no_variables["status"] == "error"
    assert bad_geometry["status"] == "error"
