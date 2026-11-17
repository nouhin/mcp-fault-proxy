"""A scripted, LLM-free stand-in for a report-writing agent.

It runs an example task, "compare the FY2025 revenue of three
companies, with year-on-year growth", over two MCP servers (web and math),
each behind its own fault proxy. It then grades its report against a
hand-written answer key with one of four outcomes: Correct, Safe gap,
Loud failure or Silent wrong.

The client has no defences on purpose: it takes the first search hit, trusts
the page, and parses numbers naively, so the demo shows what each fault does
to an unguarded pipeline.

    uv run python demo/report_client.py demo/scenarios/10-S5-one-figure-altered.yaml
"""

from __future__ import annotations

import json
import re
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path

import anyio
from mcp import ClientSession, MCPError, StdioServerParameters, stdio_client

DEMO = Path(__file__).parent
SERVERS = {"web": DEMO / "servers" / "web_server.py", "math": DEMO / "servers" / "math_server.py"}
COMPANIES = ["Aldermoor Systems", "Brightwater Foods", "Corvel Logistics"]
TIMEOUT_S = 2.0

# Prepared by hand, independently of the servers: (FY2025, FY2024, growth %).
ANSWER_KEY = {
    "Aldermoor Systems": (4812.3, 4390.1, 9.6),
    "Brightwater Foods": (2137.8, 2205.4, -3.1),
    "Corvel Logistics": (9604.0, 8871.5, 8.3),
}

REVENUE = re.compile(
    r"was EUR (?P<cur>[\d.,]+) million, compared with EUR (?P<prev>[\d.,]+) million"
)


class LoudFailure(Exception):
    """The run stops with an error the user can see."""


@dataclass
class Row:
    company: str
    fy2025: float | None = None
    fy2024: float | None = None
    growth: float | None = None
    source: str | None = None
    notes: list[str] = field(default_factory=list)


def server_command(name: str, config: Path | None, log_dir: Path) -> list[str]:
    server = [sys.executable, str(SERVERS[name])]
    if config is None:
        return server
    log = log_dir / f"{config.stem}.{name}.jsonl"
    return [sys.executable, "-m", "mcp_fault_proxy", "-q", "-c", str(config), "--log", str(log), "--", *server]


async def call(session: ClientSession, tool: str, args: dict):
    """Call a tool. Protocol errors and timeouts stop the run; tool errors do not."""
    try:
        return await session.call_tool(tool, args)
    except MCPError as e:
        raise LoudFailure(f"{tool}: {e}") from e


async def research(web: ClientSession, math: ClientSession, company: str) -> Row:
    row = Row(company)
    hits = await call(web, "web_search", {"query": f"{company} annual report 2025"})
    if hits.is_error or not hits.content:
        row.notes.append("no search results")
        return row
    url = json.loads(hits.content[0].text)["url"]

    result = await call(web, "fetch_page", {"url": url})
    page = json.loads(result.content[0].text) if not result.is_error else {}
    row.source = url
    m = REVENUE.search(page.get("text", "")) if page.get("status") == 200 else None
    if m is None:
        row.notes.append("revenue not found in the fetched page")
        return row
    # Naive parsing: drop thousands separators, read the rest as a decimal number.
    row.fy2025 = float(m["cur"].replace(",", ""))
    row.fy2024 = float(m["prev"].replace(",", ""))

    calc = await call(math, "calculator", {"expression": f"({row.fy2025} - {row.fy2024}) / {row.fy2024} * 100"})
    if calc.is_error:
        row.notes.append(f"growth not computed: {calc.content[0].text}")
    else:
        row.growth = float(calc.content[0].text)
    return row


def grade(rows: list[Row]) -> tuple[str, list[str]]:
    wrong, missing = [], []
    for row in rows:
        key = ANSWER_KEY[row.company]
        for label, got, want, tol in (
            ("FY2025", row.fy2025, key[0], 0.05),
            ("FY2024", row.fy2024, key[1], 0.05),
            ("growth", row.growth, key[2], 0.05),
        ):
            if got is None:
                missing.append(f"{row.company} {label}")
            elif abs(round(got, 1) - want) > tol:
                wrong.append(f"{row.company} {label}: stated {got:,.1f}, key {want:,.1f}")
    if wrong:
        return "Silent wrong", wrong
    if missing:
        return "Safe gap", [f"reported as missing: {m}" for m in missing]
    return "Correct", []


def render(rows: list[Row]) -> list[str]:
    lines = ["| Company | FY2025 (EUR m) | FY2024 (EUR m) | Growth | Source / notes |", "|---|---|---|---|---|"]
    for r in rows:
        f = lambda v, fmt: "n/a" if v is None else format(v, fmt)  # noqa: E731
        notes = "; ".join(filter(None, [r.source, *r.notes]))
        lines.append(f"| {r.company} | {f(r.fy2025, ',.1f')} | {f(r.fy2024, ',.1f')} | {f(r.growth, '+.1f') + ('%' if r.growth is not None else '')} | {notes} |")
    return lines


async def run_task(config: Path | None, log_dir: Path) -> tuple[list[str], str, list[str]]:
    """Run the task once. Returns (report lines, outcome, reasons)."""
    log_dir.mkdir(parents=True, exist_ok=True)
    rows: list[Row] = []
    async with AsyncExitStack() as stack:
        errlog = stack.enter_context(open(log_dir / "servers.stderr.log", "a"))
        sessions = {}
        for name in ("web", "math"):
            cmd = server_command(name, config, log_dir)
            read, write = await stack.enter_async_context(
                stdio_client(StdioServerParameters(command=cmd[0], args=cmd[1:]), errlog=errlog)
            )
            sessions[name] = await stack.enter_async_context(ClientSession(read, write, read_timeout_seconds=TIMEOUT_S))
            await sessions[name].initialize()
        try:
            for company in COMPANIES:
                rows.append(await research(sessions["web"], sessions["math"], company))
        except LoudFailure as e:
            return render(rows), "Loud failure", [f"run stopped: {e}"]
    outcome, reasons = grade(rows)
    return render(rows), outcome, reasons


def main() -> None:
    config = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    report, outcome, reasons = anyio.run(run_task, config, Path("logs"))
    print("\n".join(report))
    print(f"\nOutcome: {outcome}")
    for r in reasons:
        print(f"  - {r}")


if __name__ == "__main__":
    main()
