"""The fault-injecting proxy: the relay plus rules, faults and the fault log."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, TextIO

from .config import Config, Decision, Scheduler
from .faults import Context, Fault, RequestAction, build_fault, seeded_rng
from .proxy import Relay


class FaultLog:
    """Appends one JSON object per injected fault (JSON Lines), and a short line to stderr."""

    def __init__(self, path: str | Path | None, seed: int, echo: TextIO | None = sys.stderr) -> None:
        self.seed = seed
        self.echo = echo
        self._file = None
        if path is not None:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._file = open(path, "a", encoding="utf-8", buffering=1)

    def record(self, decision: Decision, request: dict[str, Any], phase: str, details: dict[str, Any]) -> None:
        rule = decision.rule
        entry = {
            "ts": round(time.time(), 3),
            "seed": self.seed,
            "rule": rule.id,
            "tag": rule.tag,
            "fault": rule.fault,
            "tool": decision.tool,
            "call_index": decision.call_index,
            "request_id": request.get("id"),
            "arguments": (request.get("params") or {}).get("arguments"),
            "phase": phase,
            "details": details,
        }
        if self._file is not None:
            self._file.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if self.echo is not None:
            label = f"{rule.tag} " if rule.tag else ""
            summary = ", ".join(f"{k}={_short(v)}" for k, v in details.items())
            print(
                f"[fault] {label}{rule.fault} on {decision.tool} call #{decision.call_index}"
                f" (rule {rule.id}){': ' + summary if summary else ''}",
                file=self.echo,
                flush=True,
            )

    def close(self) -> None:
        if self._file is not None:
            self._file.close()


def _short(value: Any, limit: int = 80) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 3] + "..."


class FaultProxy(Relay):
    def __init__(self, server_cmd: list[str], config: Config, log: FaultLog) -> None:
        super().__init__(server_cmd)
        self.config = config
        self.scheduler = Scheduler(config)
        self.faults: dict[str, Fault] = {
            r.id: build_fault(r.fault, r.params, r.id) for r in config.rules
        }
        self.log = log
        # Requests whose responses a fault will rewrite, by JSON-RPC id.
        self._pending: dict[Any, tuple[Fault, Context]] = {}

    async def on_client_line(self, line: bytes) -> None:
        msg = _parse(line)
        tool = _tool_call_name(msg)
        if tool is None:
            await self.send_to_server(line)
            return

        decision = self.scheduler.decide(tool)
        if decision is None:
            await self.send_to_server(line)
            return

        fault = self.faults[decision.rule.id]
        ctx = Context(
            decision=decision,
            request=msg,
            rng=seeded_rng(self.config.seed, decision.rule.id, tool, decision.call_index),
        )
        action = fault.on_request(ctx)
        await self._act(line, msg, fault, ctx, action)

    async def _act(self, line: bytes, msg: dict[str, Any], fault: Fault, ctx: Context, action: RequestAction) -> None:
        rid = msg.get("id")
        if action.kind == "drop":
            self.log.record(ctx.decision, msg, "request", {"action": "dropped, no answer will be sent"})
            return
        if action.kind == "respond":
            assert action.response is not None
            self.log.record(ctx.decision, msg, "request", action.details)
            await self._send(action.delay_s, self.send_to_client, _dump(action.response))
            return

        out = line
        if action.request is not None:
            out = _dump(action.request)
            ctx.request = action.request
        if action.rewrite_response:
            self._pending[rid] = (fault, ctx)
        if action.details:
            self.log.record(ctx.decision, msg, "request", action.details)
        await self._send(action.delay_s, self.send_to_server, out)

    async def _send(self, delay_s: float, send, data: bytes) -> None:
        # Delayed messages go in the background so other traffic keeps flowing;
        # undelayed ones are sent in order, like unfaulted traffic.
        if delay_s > 0:
            self.spawn(self._later(delay_s, send, data))
        else:
            await send(data)

    async def on_server_line(self, line: bytes) -> None:
        if self._pending:
            msg = _parse(line)
            if isinstance(msg, dict) and "method" not in msg and msg.get("id") in self._pending:
                fault, ctx = self._pending.pop(msg["id"])
                new, details = fault.on_response(msg, ctx)
                self.log.record(ctx.decision, ctx.request, "response", details)
                if new is not msg or details.get("changed"):
                    line = _dump(new)
        await self.send_to_client(line)

    @staticmethod
    async def _later(delay_s: float, send, data: bytes) -> None:
        if delay_s > 0:
            await asyncio.sleep(delay_s)
        await send(data)


def _parse(line: bytes) -> Any:
    try:
        return json.loads(line)
    except ValueError:
        return None


def _tool_call_name(msg: Any) -> str | None:
    if not isinstance(msg, dict) or msg.get("method") != "tools/call" or "id" not in msg:
        return None
    params = msg.get("params")
    name = params.get("name") if isinstance(params, dict) else None
    return name if isinstance(name, str) else None


def _dump(msg: dict[str, Any]) -> bytes:
    return json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
