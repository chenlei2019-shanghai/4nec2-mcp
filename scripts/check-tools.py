#!/usr/bin/env python3
"""List (and optionally call) the 4NEC2 MCP tools over stdio.

This is the acceptance-test helper: it spawns ``mcp_4nec2_server.py`` exactly
the way DSH does (stdio transport, venv interpreter), performs the MCP
handshake and prints every tool with its required arguments. It needs no
network and no DSH installation - only the ``mcp`` package in the venv.

Usage (from the project root)::

    .venv\\Scripts\\python.exe scripts\\check-tools.py
    .venv\\Scripts\\python.exe scripts\\check-tools.py --json
    .venv\\Scripts\\python.exe scripts\\check-tools.py --call nec_list_engines
    .venv\\Scripts\\python.exe scripts\\check-tools.py --call nec_run ^
        --arg path=478MHz_8El_Yagi.nec --arg freq_mhz=478.875
    .venv\\Scripts\\python.exe scripts\\check-tools.py --nec2-dir "D:\\4nec2"

``--arg name=value`` is the shell-proof way to pass arguments: PowerShell 5.1
strips the inner double quotes of a ``--args '{...}'`` literal before the native
command sees them. ``--args`` is kept for callers that pass argv directly
(Python ``subprocess``, CI jobs) with no cmd.exe/PowerShell in between. Values
of ``--arg`` are parsed as JSON when possible, otherwise passed as strings.

Exit code is 0 when the handshake and every requested call succeed, 1 when a
call returns an MCP error or the server cannot be started.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "mcp_4nec2_server.py"
VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"


def _interpreter() -> str:
    """Prefer the project venv (what DSH uses), fall back to this interpreter."""
    return str(VENV_PYTHON if VENV_PYTHON.is_file() else Path(sys.executable))


def _first_line(text: str | None, limit: int = 110) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line[:limit]


def _required(schema: dict | None) -> list[str]:
    if not schema:
        return []
    return list(schema.get("required") or [])


def _props(schema: dict | None) -> dict:
    if not schema:
        return {}
    return schema.get("properties") or {}


async def run(args: argparse.Namespace) -> int:
    if not SERVER.is_file():
        print(f"[FAIL] server entry not found: {SERVER}")
        return 1

    params = StdioServerParameters(
        command=_interpreter(),
        args=[str(SERVER), "--4nec2-dir", args.nec2_dir],
    )
    print(f"spawning : {params.command} {' '.join(params.args)}")

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            info = init.serverInfo
            print(f"connected: {info.name} v{info.version} (MCP {init.protocolVersion})")

            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print(f"tools    : {len(names)}")
            for name in names:
                print(f"  - {name}")

            if args.json:
                payload = {
                    "server": {"name": info.name, "version": info.version},
                    "protocolVersion": init.protocolVersion,
                    "tools": [
                        {
                            "name": t.name,
                            "description": t.description,
                            "required": _required(t.inputSchema),
                            "properties": sorted(_props(t.inputSchema)),
                        }
                        for t in sorted(tools.tools, key=lambda x: x.name)
                    ],
                }
                print(json.dumps(payload, ensure_ascii=False, indent=2))

            if not args.call:
                return 0

            try:
                arguments = json.loads(args.args) if args.args else {}
            except json.JSONDecodeError as exc:
                print(f"[FAIL] --args is not valid JSON: {exc}")
                return 1
            if not isinstance(arguments, dict):
                print("[FAIL] --args must be a JSON object")
                return 1
            for item in args.arg or []:
                key, sep, raw = item.partition("=")
                if not sep or not key:
                    print(f"[FAIL] --arg expects name=value, got {item!r}")
                    return 1
                try:
                    arguments[key] = json.loads(raw)
                except json.JSONDecodeError:
                    arguments[key] = raw
            if args.call not in names:
                print(f"[FAIL] unknown tool {args.call!r}; available: {', '.join(names)}")
                return 1

            print(f"\ncalling  : {args.call}({', '.join(f'{k}={v!r}' for k, v in arguments.items()) or ''})")
            result = await session.call_tool(args.call, arguments)
            texts = [getattr(c, "text", "") for c in result.content if getattr(c, "type", "") == "text"]
            body = "\n".join(t for t in texts if t)
            try:
                pretty = json.dumps(json.loads(body), ensure_ascii=False, indent=2)
            except (json.JSONDecodeError, TypeError):
                pretty = body
            print(pretty if pretty else "<empty response>")
            if result.isError:
                print("[FAIL] tool reported an error")
                return 1
            print("[OK] tool call succeeded")
            return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="4NEC2 MCP server smoke test")
    parser.add_argument("--nec2-dir", default=r"C:\4nec2", help="4NEC2 installation directory")
    parser.add_argument("--call", default="", help="tool name to invoke after listing")
    parser.add_argument("--args", default="", help="JSON object passed to --call")
    parser.add_argument(
        "--arg",
        action="append",
        metavar="NAME=VALUE",
        help="single argument for --call (repeatable; shell-proof alternative to --args)",
    )
    parser.add_argument("--json", action="store_true", help="dump tool schemas as JSON")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI diagnostics
        print(f"[FAIL] {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
