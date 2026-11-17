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

import copy
import hashlib
import json
import random
import re
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


class RewriteArgs(Fault):
    """Rewrite one argument of the call before forwarding it (``re.sub``).

    Example: turn an FY2025 report URL into the FY2024 one, so the server
    honestly returns last year's report for this year's request.
    """

    name = "rewrite_args"
    schema = {"argument": ((str,), ...), "pattern": ((str,), ...), "replacement": ((str,), ...)}

    def validate(self, rule_id: str) -> None:
        try:
            re.compile(self.p["pattern"])
        except re.error as e:
            raise ConfigError(f"rule {rule_id!r}: rewrite_args.pattern: {e}") from e

    def on_request(self, ctx: Context) -> RequestAction:
        request = copy.deepcopy(ctx.request)
        args = request.get("params", {}).get("arguments") or {}
        before = args.get(self.p["argument"])
        if not isinstance(before, str):
            return RequestAction(details={"changed": False, "note": f"no string argument {self.p['argument']!r}"})
        after = re.sub(self.p["pattern"], self.p["replacement"], before)
        args[self.p["argument"]] = after
        return RequestAction(
            request=request if after != before else None,
            details={"changed": after != before, "argument": self.p["argument"], "from": before, "to": after},
        )


# --- faults on the result: the call succeeds but the content is wrong -----------------

# Figures such as 12,345.6 or 4.82; plain integers (years, page numbers) are left alone.
NUMBER = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+"


class ResultFault(Fault):
    """Rewrites a successful tool result: its text content blocks and its structured content.

    Text blocks that hold JSON are parsed, rewritten and dumped again, so the
    text and the structured copy of a result stay consistent. With ``field``
    set, only values under keys of that name are touched (e.g. a page's
    ``text`` but not its ``url``). Server errors are passed through untouched.
    """

    schema = {"field": ((str, type(None)), None)}

    def on_response(self, response: dict[str, Any], ctx: Context) -> tuple[dict[str, Any], dict[str, Any]]:
        result = response.get("result")
        if not isinstance(result, dict) or result.get("isError"):
            return response, {"changed": False, "note": "server returned an error; passed through"}
        changes: list[tuple[str, str]] = []
        seed = ctx.rng.getrandbits(64)
        new = self.rewrite(copy.deepcopy(result), seed, changes)
        unique = list(dict.fromkeys(changes))
        details: dict[str, Any] = {"changed": bool(unique)}
        if unique:
            details["changes"] = [{"from": a, "to": b} for a, b in unique[:5]]
        return ({**response, "result": new} if unique else response), details

    def rewrite(self, result: dict[str, Any], seed: int, changes: list) -> dict[str, Any]:
        for block in result.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                block["text"] = self._text(block["text"], seed, changes)
        if "structuredContent" in result:
            result["structuredContent"] = self._walk(result["structuredContent"], seed, changes, False)
        return result

    def _text(self, text: str, seed: int, changes: list) -> str:
        try:
            parsed = json.loads(text)
        except ValueError:
            return self._leaf(text, seed, changes)
        new = self._walk(parsed, seed, changes, False)
        return text if new == parsed else json.dumps(new, indent=2, ensure_ascii=False)

    def _walk(self, value: Any, seed: int, changes: list, inside: bool) -> Any:
        active = inside or self.p["field"] is None
        if isinstance(value, dict):
            return {k: self._walk(v, seed, changes, inside or k == self.p["field"]) for k, v in value.items()}
        if isinstance(value, list):
            return [self._walk(v, seed, changes, inside) for v in value]
        if active and (isinstance(value, str) or isinstance(value, int | float) and not isinstance(value, bool)):
            return self._leaf(value, seed, changes)
        return value

    def _leaf(self, value: Any, seed: int, changes: list) -> Any:
        # A fresh generator per leaf, so the text and structured copies change the same way.
        new = self.leaf(value, random.Random(seed))
        if new != value:
            changes.append(_diff(value, new))
        return new

    def leaf(self, value: Any, rng: random.Random) -> Any:
        return value


class AlterNumber(ResultFault):
    """Change one figure by a few percent, keeping its format (S5: one figure altered)."""

    name = "alter_number"
    schema = {
        **ResultFault.schema,
        "pattern": ((str,), NUMBER),
        "min_change": ((int, float), 0.05),
        "max_change": ((int, float), 0.15),
    }

    def validate(self, rule_id: str) -> None:
        if not 0 <= self.p["min_change"] <= self.p["max_change"]:
            raise ConfigError(f"rule {rule_id!r}: need 0 <= min_change <= max_change")

    def _factor(self, rng: random.Random) -> float:
        sign = rng.choice((-1, 1))
        return 1 + sign * rng.uniform(self.p["min_change"], self.p["max_change"])

    def leaf(self, value: Any, rng: random.Random) -> Any:
        if not isinstance(value, str):
            factor = self._factor(rng)
            return round(value * factor) if isinstance(value, int) else round(value * factor, 6)
        matches = list(re.finditer(self.p["pattern"], value))
        if not matches:
            return value
        m = matches[rng.randrange(len(matches))]
        token = m.group(0)
        new = _format_like(float(token.replace(",", "")) * self._factor(rng), token)
        return value[: m.start()] + new + value[m.end() :]


class ReformatNumbers(ResultFault):
    """Write figures in the European style, 12,345.6 -> 12.345,6 (S8: messy number formats)."""

    name = "reformat_numbers"
    schema = {**ResultFault.schema, "pattern": ((str,), NUMBER)}

    def leaf(self, value: Any, rng: random.Random) -> Any:
        if not isinstance(value, str):
            return value
        swap = str.maketrans({",": ".", ".": ","})
        return re.sub(self.p["pattern"], lambda m: m.group(0).translate(swap), value)


class Truncate(ResultFault):
    """Cut text off after a fraction of its length (S2: page cut off)."""

    name = "truncate"
    schema = {**ResultFault.schema, "keep": ((int, float), 0.3)}

    def validate(self, rule_id: str) -> None:
        if not 0 <= self.p["keep"] < 1:
            raise ConfigError(f"rule {rule_id!r}: truncate.keep must be in [0, 1)")

    def leaf(self, value: Any, rng: random.Random) -> Any:
        if not isinstance(value, str) or len(value) < 2:
            return value
        return value[: int(len(value) * self.p["keep"])]


class ReplaceText(ResultFault):
    """Replace text with fixed content, e.g. a login page served with status 200 (S3)."""

    name = "replace_text"
    schema = {**ResultFault.schema, "text": ((str,), ...)}

    def leaf(self, value: Any, rng: random.Random) -> Any:
        return self.p["text"] if isinstance(value, str) else value


class EmptyResult(ResultFault):
    """Return a successful but empty result (S1: no search hits)."""

    name = "empty_result"
    schema = {}

    def rewrite(self, result: dict[str, Any], seed: int, changes: list) -> dict[str, Any]:
        if result.get("content"):
            changes.append((f"{len(result['content'])} content blocks", "0 content blocks"))
        result["content"] = []
        sc = result.get("structuredContent")
        if isinstance(sc, dict):
            result["structuredContent"] = {k: [] if isinstance(v, list) else v for k, v in sc.items()}
        return result


class InjectItem(ResultFault):
    """Put an extra item first in a list result (S6: unofficial site ranked first)."""

    name = "inject_item"
    schema = {"item": ((dict,), ...)}

    def rewrite(self, result: dict[str, Any], seed: int, changes: list) -> dict[str, Any]:
        item = self.p["item"]
        result["content"] = [
            {"type": "text", "text": json.dumps(item, indent=2, ensure_ascii=False)},
            *(result.get("content") or []),
        ]
        sc = result.get("structuredContent")
        if isinstance(sc, dict):
            for k, v in sc.items():
                if isinstance(v, list):
                    sc[k] = [item, *v]
                    break
        changes.append(("", _clip(item)))
        return result


def _format_like(value: float, token: str) -> str:
    """Format ``value`` with the same decimals and thousands separators as ``token``."""
    decimals = len(token.split(".", 1)[1]) if "." in token else 0
    return f"{value:,.{decimals}f}" if "," in token else f"{value:.{decimals}f}"


def _diff(old: Any, new: Any, context: int = 30) -> tuple[str, str]:
    """The changed part of a value, with a little context, for the log."""
    if not (isinstance(old, str) and isinstance(new, str)):
        return _clip(old), _clip(new)
    p = 0
    while p < min(len(old), len(new)) and old[p] == new[p]:
        p += 1
    s = 0
    while s < min(len(old), len(new)) - p and old[-1 - s] == new[-1 - s]:
        s += 1
    head = ("..." if p > context else "") + old[max(0, p - context) : p]
    tail = old[len(old) - s : len(old) - s + context] + ("..." if s > context else "")
    return _clip(head + old[p : len(old) - s] + tail), _clip(head + new[p : len(new) - s] + tail)


def _clip(value: Any, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 3] + "..."


FAULTS: dict[str, type[Fault]] = {
    f.name: f
    for f in (
        Latency, Error, Drop, RewriteArgs,
        AlterNumber, ReformatNumbers, Truncate, ReplaceText, EmptyResult, InjectItem,
    )
}


def build_fault(fault: str, params: dict[str, Any], rule_id: str) -> Fault:
    if fault not in FAULTS:
        raise ConfigError(f"rule {rule_id!r}: unknown fault {fault!r}; known: {sorted(FAULTS)}")
    return FAULTS[fault](params, rule_id)
