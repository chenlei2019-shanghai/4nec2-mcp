import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PY = r"C:\4nec2-mcp-server\.venv\Scripts\python.exe"
SERVER = r"C:\4nec2-mcp-server\mcp_4nec2_server.py"


async def main() -> None:
    params = StdioServerParameters(
        command=PY,
        args=[SERVER, "--4nec2-dir", r"C:\4nec2"],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print("server:", init.serverInfo.name, init.serverInfo.version)
            tools = await session.list_tools()
            print("tools:", [t.name for t in tools.tools])
            for name, args in [
                ("nec_list_engines", {}),
                ("nec_run", {"path": r"C:\4nec2\models\478MHz_8El_Yagi.nec"}),
                ("nec_describe", {"path": r"C:\4nec2\models\478MHz_8El_Yagi.nec"}),
                (
                    "nec_sweep",
                    {
                        "path": r"C:\4nec2\models\478MHz_8El_Yagi.nec",
                        "start_mhz": 470,
                        "stop_mhz": 480,
                        "step_mhz": 5,
                    },
                ),
            ]:
                result = await session.call_tool(name, args)
                text = result.content[0].text if result.content else ""
                parsed = json.loads(text)
                print(f"\n== {name} == status={parsed.get('status')}")
                print(json.dumps(parsed, ensure_ascii=False, indent=1)[:1200])


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())
