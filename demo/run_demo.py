"""Run every scenario in demo/scenarios and write a readable log to demo/demo-run.log.

    uv run python demo/run_demo.py            # all scenarios
    uv run python demo/run_demo.py 10 11      # scenarios whose name starts with 10 or 11
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

import anyio

from report_client import run_task

DEMO = Path(__file__).parent
LOGS = DEMO.parent / "logs"
OUT = DEMO / "demo-run.log"


def faults(log_dir: Path) -> list[dict]:
    entries = []
    for path in sorted(log_dir.glob("*.jsonl")):
        entries += [json.loads(line) for line in path.read_text().splitlines()]
    return sorted(entries, key=lambda e: e["ts"])


def describe(e: dict) -> str:
    d = e["details"]
    args = ", ".join(f"{k}={v!r}" for k, v in (e["arguments"] or {}).items())
    head = f"[{e['tag']}] {e['fault']} on {e['tool']} call #{e['call_index']} ({args})"
    if e["fault"] == "latency":
        return f"{head}: +{d['delay_ms']} ms"
    if e["fault"] == "error":
        return f"{head}: {d['mode']} error {d['message']!r}"
    if e["fault"] == "drop":
        return f"{head}: {d['action']}"
    if "from" in d:
        return f"{head}: {d['argument']} {d['from']!r} -> {d['to']!r}"
    changes = d.get("changes") or [{"from": "", "to": d.get("note", "no change")}]
    return f"{head}: " + "; ".join(f"{c['from']!r} -> {c['to']!r}" for c in changes)


async def run(scenario: Path, log_dir: Path):
    shutil.rmtree(log_dir, ignore_errors=True)
    t0 = time.monotonic()
    report, outcome, reasons = await run_task(scenario, log_dir)
    return report, outcome, reasons, faults(log_dir), time.monotonic() - t0


async def main(prefixes: list[str]) -> None:
    out: list[str] = []

    def emit(line: str = "") -> None:
        print(line, flush=True)
        out.append(line)

    scenarios = sorted(p for p in (DEMO / "scenarios").glob("*.yaml") if not prefixes or p.name.startswith(tuple(prefixes)))
    summary = []
    emit("Demo run of mcp-fault-proxy: the scripted report agent client against toy web and math servers.")
    emit("Each scenario puts both servers behind the proxy with the scenario's configuration.")
    for scenario in scenarios:
        title = scenario.read_text().splitlines()[0].lstrip("# ")
        report, outcome, reasons, injected, secs = await run(scenario, LOGS / scenario.stem)
        emit()
        emit(f"=== {scenario.stem} ===")
        emit(title)
        emit("Faults injected:" if injected else "Faults injected: none")
        for e in injected:
            emit("  " + describe(e))
        emit("Report:")
        for line in report:
            emit("  " + line)
        emit(f"Outcome: {outcome}  ({secs:.1f} s)")
        for r in reasons:
            emit(f"  - {r}")
        summary.append((scenario.stem, len(injected), outcome))

        if "random" in scenario.stem:
            _, outcome2, _, injected2, _ = await run(scenario, LOGS / (scenario.stem + "-rerun"))
            strip = lambda es: [{k: v for k, v in e.items() if k != "ts"} for e in es]  # noqa: E731
            same = strip(injected) == strip(injected2) and outcome == outcome2
            emit(f"Rerun with the same configuration: {'identical faults and outcome' if same else 'DIFFERENT'}")

    emit()
    emit("=== Summary ===")
    emit(f"{'scenario':34} {'faults':>6}  outcome")
    for name, n, outcome in summary:
        emit(f"{name:34} {n:>6}  {outcome}")
    if not prefixes:
        OUT.write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    anyio.run(main, sys.argv[1:])
