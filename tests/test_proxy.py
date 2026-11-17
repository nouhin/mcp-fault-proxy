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


async def test_wrong_but_plausible_results(tmp_path):
    rules = [
        {"tool": "page", "fault": "alter_number", "field": "text", "nth": 1},
        {"tool": "page", "fault": "reformat_numbers", "field": "text", "nth": 2},
        {"tool": "page", "fault": "rewrite_args", "argument": "url", "pattern": "2025", "replacement": "2024", "nth": 3},
        {"tool": "add", "fault": "alter_number", "nth": 1},
        {"tool": "search", "fault": "empty_result", "nth": 1},
        {"tool": "search", "fault": "inject_item", "item": {"title": "Fan wiki", "url": "https://wiki"}, "nth": 2},
    ]
    async with connect(tmp_path, rules) as s:
        pages = [json.loads((await s.call_tool("page", {"url": "fy2025"})).content[0].text) for _ in range(3)]
        added = await s.call_tool("add", {"a": 100.0, "b": 0.5})
        empty = await s.call_tool("search", {"query": "ACME"})
        ranked = await s.call_tool("search", {"query": "ACME"})

    original = "Revenue was EUR 1,234.5 million, up from 1,100.0 million."
    altered, european, older = pages
    assert altered["status"] == 200 and altered["url"] == "fy2025" and altered["text"] != original
    assert altered["text"].startswith("Revenue was EUR ") and altered["text"].endswith(" million.")
    assert european["text"] == "Revenue was EUR 1.234,5 million, up from 1.100,0 million."
    assert older["url"] == "fy2024"
    assert not added.is_error and added.structured_content["result"] != 100.5
    assert float(added.content[0].text) == added.structured_content["result"]
    assert not empty.is_error and empty.content == [] and empty.structured_content == {"result": []}
    assert [h["title"] for h in ranked.structured_content["result"]][0] == "Fan wiki"
    assert all(e["details"].get("changed") for e in fault_log(tmp_path))


async def test_demo_baseline_is_correct_and_altered_figure_is_silently_wrong(tmp_path):
    demo = Path(__file__).parent.parent / "demo"
    sys.path.insert(0, str(demo))
    from report_client import run_task

    _, outcome, _ = await run_task(demo / "scenarios" / "00-baseline.yaml", tmp_path / "a")
    assert outcome == "Correct"
    _, outcome, reasons = await run_task(demo / "scenarios" / "10-S5-one-figure-altered.yaml", tmp_path / "b")
    assert outcome == "Silent wrong" and any("Brightwater Foods FY2025" in r for r in reasons)
