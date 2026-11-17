"""End-to-end checks: a real MCP client talks to a toy server through the proxy."""

import json
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import yaml
from mcp import ClientSession, MCPError, StdioServerParameters, stdio_client

TOY = [sys.executable, str(Path(__file__).parent / "servers" / "toy_server.py")]


@asynccontextmanager
async def connect(tmp_path=None, rules=None, seed=0, timeout=10.0):
    """Connect to the toy server, through the proxy when ``rules`` is given."""
    cmd = TOY
    if rules is not None:
        cfg = tmp_path / f"faults-{seed}.yaml"
        cfg.write_text(yaml.safe_dump({"seed": seed, "log": str(cfg) + ".jsonl", "rules": rules}))
        cmd = [sys.executable, "-m", "mcp_fault_proxy", "-q", "-c", str(cfg), "--", *TOY]
    params = StdioServerParameters(command=cmd[0], args=cmd[1:])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=timeout) as s:
            await s.initialize()
            yield s


def fault_log(tmp_path, seed=0):
    path = tmp_path / f"faults-{seed}.yaml.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


async def test_normal_traffic_is_unchanged(tmp_path):
    calls = [("add", {"a": 2, "b": 3.5}), ("search", {"query": "ACME"}), ("page", {"url": "u"})]

    async def collect(**kw):
        async with connect(**kw) as s:
            tools = (await s.list_tools()).model_dump()
            return tools, [(await s.call_tool(n, a)).model_dump() for n, a in calls]

    assert await collect(tmp_path=tmp_path, rules=[]) == await collect()


async def test_latency(tmp_path):
    rules = [{"tool": "add", "fault": "latency", "nth": 2, "delay_ms": 600}]
    async with connect(tmp_path, rules) as s:
        t0 = time.monotonic()
        await s.call_tool("add", {"a": 1, "b": 2})
        t1 = time.monotonic()
        res = await s.call_tool("add", {"a": 1, "b": 2})
        t2 = time.monotonic()
    assert t1 - t0 < 0.5 and t2 - t1 >= 0.6 and res.structured_content == {"result": 3}
    assert [(e["fault"], e["call_index"]) for e in fault_log(tmp_path)] == [("latency", 2)]


async def test_error_and_drop(tmp_path):
    rules = [
        {"tool": "add", "fault": "error", "nth": 1, "message": "Rate limit exceeded"},
        {"tool": "add", "fault": "error", "nth": 2, "mode": "tool", "message": "down"},
        {"tool": "add", "fault": "drop", "nth": 3},
    ]
    async with connect(tmp_path, rules, timeout=1.0) as s:
        with pytest.raises(MCPError, match="Rate limit exceeded"):
            await s.call_tool("add", {"a": 1, "b": 2})
        assert (await s.call_tool("add", {"a": 1, "b": 2})).is_error
        with pytest.raises(MCPError, match="timed out"):
            await s.call_tool("add", {"a": 1, "b": 2})
        assert not (await s.call_tool("add", {"a": 1, "b": 2})).is_error
    assert [e["fault"] for e in fault_log(tmp_path)] == ["error", "error", "drop"]


async def test_same_seed_same_faults(tmp_path):
    rules = [{"tool": "*", "fault": "error", "rate": 0.4, "mode": "tool"}]

    async def run():
        async with connect(tmp_path, rules, seed=7) as s:
            return [(await s.call_tool("add", {"a": i, "b": 0})).is_error for i in range(20)]

    first, second = await run(), await run()
    assert first == second and 0 < sum(first) < 20
