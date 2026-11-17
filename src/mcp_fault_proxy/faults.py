"""Fault types.

A fault acts on a ``tools/call`` request when a rule fires. It can:

* delay the request before forwarding it (latency),
* answer in place of the server and never forward the request (error),
* swallow the request so no answer ever comes (drop),
* rewrite the request before forwarding it, or
* rewrite the server's response before it reaches the client.

Every fault draws any randomness it needs from ``ctx.rng``, which is seeded
from the same inputs as the firing decision, so a fault does the same thing
every time its rule fires on the same call.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from typing import Any, ClassVar

from .config import ConfigError, Decision


@dataclass
class Context:
    decision: Decision
    request: dict[str, Any]
    rng: random.Random

    @property
    def params(self) -> dict[str, Any]:
        return self.decision.rule.params


@dataclass
class RequestAction:
    """What the proxy does with the request."""

    kind: str = "forward"  # forward | respond | drop
    delay_s: float = 0.0
    request: dict[str, Any] | None = None  # rewritten request, if any
    response: dict[str, Any] | None = None  # answer sent in place of the server's
    rewrite_response: bool = False  # call Fault.on_response on the server's answer
    details: dict[str, Any] = field(default_factory=dict)


def seeded_rng(seed: int, rule_id: str, tool: str, call_index: int) -> random.Random:
    digest = hashlib.sha256(f"rng|{seed}|{rule_id}|{tool}|{call_index}".encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


class Fault:
    name: ClassVar[str]
    # Parameter name -> (accepted types, default). A default of ... means required.
    schema: ClassVar[dict[str, tuple[tuple[type, ...], Any]]] = {}

    def __init__(self, params: dict[str, Any], rule_id: str) -> None:
        unknown = set(params) - set(self.schema)
        if unknown:
            raise ConfigError(f"rule {rule_id!r}: unknown parameters for {self.name}: {sorted(unknown)}")
        self.p: dict[str, Any] = {}
        for key, (types, default) in self.schema.items():
            if key not in params:
                if default is ...:
                    raise ConfigError(f"rule {rule_id!r}: {self.name} needs parameter {key!r}")
                self.p[key] = default
                continue
            value = params[key]
            if isinstance(value, bool) and bool not in types or not isinstance(value, types):
                names = "/".join(t.__name__ for t in types)
                raise ConfigError(f"rule {rule_id!r}: {self.name}.{key} must be {names}")
            self.p[key] = value
        self.validate(rule_id)

    def validate(self, rule_id: str) -> None:
        """Extra checks on parameters."""

    def on_request(self, ctx: Context) -> RequestAction:
        return RequestAction(rewrite_response=True)

    def on_response(self, response: dict[str, Any], ctx: Context) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return the response to send to the client and details for the log."""
        return response, {}


# --- faults on the call itself --------------------------------------------------


class Latency(Fault):
    """Hold the request for ``delay_ms`` (plus up to ``jitter_ms``) before forwarding it.

    A delay longer than the client's timeout turns this into a timeout.
    """

    name = "latency"
    schema = {"delay_ms": ((int, float), ...), "jitter_ms": ((int, float), 0)}

    def validate(self, rule_id: str) -> None:
        if self.p["delay_ms"] < 0 or self.p["jitter_ms"] < 0:
            raise ConfigError(f"rule {rule_id!r}: latency delays must be non-negative")

    def on_request(self, ctx: Context) -> RequestAction:
        delay_ms = self.p["delay_ms"] + ctx.rng.uniform(0, self.p["jitter_ms"])
        return RequestAction(delay_s=delay_ms / 1000, details={"delay_ms": round(delay_ms, 1)})


class Error(Fault):
    """Answer with an error in place of the server; the server never sees the call.

    ``mode: jsonrpc`` sends a JSON-RPC error (a protocol-level failure, e.g. a
    rate limit enforced in front of the server). ``mode: tool`` sends a tool
    result with ``isError: true`` (the tool itself reports the failure).
    """

    name = "error"
    schema = {
        "mode": ((str,), "jsonrpc"),
        "code": ((int,), -32000),
        "message": ((str,), "Rate limit exceeded, retry after 30 seconds"),
    }

    def validate(self, rule_id: str) -> None:
        if self.p["mode"] not in ("jsonrpc", "tool"):
            raise ConfigError(f"rule {rule_id!r}: error.mode must be 'jsonrpc' or 'tool'")

    def on_request(self, ctx: Context) -> RequestAction:
        rid = ctx.request.get("id")
        if self.p["mode"] == "jsonrpc":
            response = {
                "jsonrpc": "2.0",
                "id": rid,
                "error": {"code": self.p["code"], "message": self.p["message"]},
            }
        else:
            response = {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {"content": [{"type": "text", "text": self.p["message"]}], "isError": True},
            }
        return RequestAction(
            kind="respond",
            response=response,
            details={"mode": self.p["mode"], "message": self.p["message"]},
        )


class Drop(Fault):
    """Swallow the request: the server never sees it and the client never gets an answer."""

    name = "drop"

    def on_request(self, ctx: Context) -> RequestAction:
        return RequestAction(kind="drop")


FAULTS: dict[str, type[Fault]] = {f.name: f for f in (Latency, Error, Drop)}


def build_fault(fault: str, params: dict[str, Any], rule_id: str) -> Fault:
    if fault not in FAULTS:
        raise ConfigError(f"rule {rule_id!r}: unknown fault {fault!r}; known: {sorted(FAULTS)}")
    return FAULTS[fault](params, rule_id)
