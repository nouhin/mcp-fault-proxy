"""Run the report task with a real LLM (any OpenAI-compatible server, e.g. LM Studio)
calling the toy MCP tools through the fault proxy, and grade it like the scripted client.

    uv run python demo/llm_agent.py [scenario prefixes...]   # default: all scenarios
    LLM_URL=http://localhost:1234/v1 LLM_MODEL=qwen/qwen3.5-9b RUNS=1 PARALLEL=4
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path

import httpx
from mcp import ClientSession, MCPError, StdioServerParameters, stdio_client

from report_client import ANSWER_KEY, COMPANIES, DEMO, server_command

URL = os.environ.get("LLM_URL", "http://localhost:1234/v1")
MODEL = os.environ.get("LLM_MODEL", "qwen/qwen3.5-9b")
RUNS = int(os.environ.get("RUNS", "1"))
PARALLEL = int(os.environ.get("PARALLEL", "4"))
MAX_STEPS = 30
TOOL_TIMEOUT_S = 2.0

TASK = (
    "Write a short report comparing the FY2025 revenue of three companies: "
    + ", ".join(COMPANIES)
    + ". Include FY2024 revenue and year-on-year growth for each. Use official annual reports, "
    "found with web_search and read with fetch_page; compute growth with calculator. "
    "Report figures in EUR million. If a figure cannot be found reliably, say so instead of guessing. "
    "End your answer with a JSON code block mapping each company name to "
    '{"fy2025": number|null, "fy2024": number|null, "growth_pct": number|null}.'
)


async def chat(client: httpx.AsyncClient, messages: list, tools: list) -> dict:
    r = await client.post(f"{URL}/chat/completions", json={"model": MODEL, "messages": messages, "tools": tools})
    r.raise_for_status()
    return r.json()["choices"][0]["message"]


async def run_once(scenario: Path, log_dir: Path) -> dict:
    stats = {"tool_calls": 0, "tool_errors": 0, "steps": 0}
    async with AsyncExitStack() as stack:
        log_dir.mkdir(parents=True, exist_ok=True)
        errlog = stack.enter_context(open(log_dir / "servers.stderr.log", "a"))
        route, tools = {}, []
        for name in ("web", "math"):
            cmd = server_command(name, scenario, log_dir)
            read, write = await stack.enter_async_context(
                stdio_client(StdioServerParameters(command=cmd[0], args=cmd[1:]), errlog=errlog)
            )
            s = await stack.enter_async_context(ClientSession(read, write, read_timeout_seconds=TOOL_TIMEOUT_S))
            await s.initialize()
            for t in (await s.list_tools()).tools:
                route[t.name] = s
                tools.append({"type": "function", "function": {"name": t.name, "description": t.description or "", "parameters": t.input_schema}})

        http = await stack.enter_async_context(httpx.AsyncClient(timeout=600))
        messages = [{"role": "user", "content": TASK}]
        for _ in range(MAX_STEPS):
            stats["steps"] += 1
            msg = await chat(http, messages, tools)
            messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
            if not msg.get("tool_calls"):
                (log_dir / "final.md").write_text(json.dumps(msg, indent=2, ensure_ascii=False))
                # Some servers (LM Studio with Qwen) put the whole answer in reasoning_content.
                return {**stats, "final": msg.get("content") or msg.get("reasoning_content") or ""}
            for call in msg["tool_calls"]:
                stats["tool_calls"] += 1
                fn = call["function"]
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                    res = await route[fn["name"]].call_tool(fn["name"], args)
                    text = "\n".join(c.text for c in res.content if getattr(c, "text", None) is not None)
                    if res.is_error:
                        stats["tool_errors"] += 1
                        text = f"Tool error: {text}"
                except MCPError as e:
                    stats["tool_errors"] += 1
                    text = f"Error: {e}"
                except (KeyError, ValueError) as e:
                    stats["tool_errors"] += 1
                    text = f"Invalid tool call: {e}"
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": text or "(empty result)"})
        return {**stats, "final": None}


def grade(final: str | None) -> tuple[str, list[str]]:
    if final is None:
        return "Hang", [f"no final answer within {MAX_STEPS} steps"]
    blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", final, re.S)
    try:
        data = json.loads(blocks[-1])
    except (IndexError, ValueError):
        return "Loud failure", ["no parseable JSON summary in the final answer"]
    wrong, missing = [], []
    for company, (k25, k24, kg) in ANSWER_KEY.items():
        row = next((v for k, v in data.items() if company.lower() in k.lower()), {}) or {}
        for label, got, want, tol in (("FY2025", row.get("fy2025"), k25, 0.05), ("FY2024", row.get("fy2024"), k24, 0.05), ("growth", row.get("growth_pct"), kg, 0.15)):
            if got is None:
                missing.append(f"{company} {label}")
            elif not isinstance(got, int | float) or abs(got - want) > tol:
                wrong.append(f"{company} {label}: stated {got}, key {want}")
    if wrong:
        return "Silent wrong", wrong
    if len(missing) == 9:
        return "Loud failure", ["no figures reported"]
    if missing:
        return "Safe gap", [f"missing: {m}" for m in missing]
    return "Correct", []


async def main(prefixes: list[str]) -> None:
    scenarios = sorted(p for p in (DEMO / "scenarios").glob("*.yaml") if not prefixes or p.name.startswith(tuple(prefixes)))
    jobs = [(s, i) for s in scenarios for i in range(RUNS)]
    sem = asyncio.Semaphore(PARALLEL)
    results: dict[tuple, tuple] = {}

    async def job(s: Path, i: int) -> None:
        async with sem:
            t0 = time.monotonic()
            try:
                out = await run_once(s, DEMO.parent / "logs" / "llm" / f"{s.stem}-{i}")
                outcome, reasons = grade(out["final"])
            except Exception as e:  # noqa: BLE001 - report and keep going
                out, outcome, reasons = {"tool_calls": 0, "tool_errors": 0, "steps": 0}, "Harness error", [repr(e)]
            results[(s.stem, i)] = (outcome, reasons, out, time.monotonic() - t0)
            print(f"{s.stem} run {i}: {outcome} ({out['tool_calls']} calls, {out['tool_errors']} errors, {time.monotonic() - t0:.0f} s)", flush=True)
            for r in reasons[:4]:
                print(f"    - {r}", flush=True)

    await asyncio.gather(*(job(s, i) for s, i in jobs))
    print(f"\n=== Summary ({MODEL}, {RUNS} run(s) per scenario) ===")
    for s in scenarios:
        outs = [results[(s.stem, i)][0] for i in range(RUNS)]
        print(f"{s.stem:34} {', '.join(outs)}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
