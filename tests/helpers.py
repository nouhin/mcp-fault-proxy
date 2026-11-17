"""Shared helpers: start servers directly or behind the proxy, from a real MCP client."""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, stdio_client

SERVERS = Path(__file__).parent / "servers"
TOY = [sys.executable, str(SERVERS / "toy_server.py")]


def proxied(server_cmd: list[str], *proxy_args: str) -> list[str]:
    """Command that runs ``server_cmd`` behind the proxy."""
    return [sys.executable, "-m", "mcp_fault_proxy", *proxy_args, "--", *server_cmd]


@asynccontextmanager
async def session(cmd: list[str], timeout: float = 10.0):
    params = StdioServerParameters(command=cmd[0], args=cmd[1:])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=timeout) as s:
            await s.initialize()
            yield s
