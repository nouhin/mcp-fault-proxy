"""Fault configuration and the seeded decision of when a fault fires.

A configuration is a seed plus a list of rules. Each rule names a tool (or
``*``), a fault, how often it fires and the fault's own parameters::

    seed: 7
    log: logs/faults.jsonl
    rules:
      - id: slow-search
        tag: L1            # optional label, e.g. a fault ID
        tool: web_search
        fault: latency
        rate: 0.5          # probability for each matching call ...
        nth: [3]           # ... and/or these calls always fire (1-based)
        max_count: 2       # optional cap on injections by this rule
        delay_ms: 1500     # fault parameter

Whether a rule fires on a call depends only on the seed, the rule id, the tool
name and the call's position among calls to that tool. It does not depend on
timing or on other rules, so the same configuration and the same sequence of
calls always inject the same faults.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

RULE_KEYS = {"id", "tag", "tool", "fault", "rate", "nth", "max_count"}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Rule:
    id: str
    tool: str
    fault: str
    rate: float = 0.0
    nth: frozenset[int] = frozenset()
    max_count: int | None = None
    tag: str | None = None
    params: dict[str, Any] = field(default_factory=dict)

    def matches(self, tool: str) -> bool:
        return self.tool in ("*", tool)


@dataclass(frozen=True)
class Config:
    seed: int = 0
    rules: tuple[Rule, ...] = ()
    log: str | None = None


@dataclass(frozen=True)
class Decision:
    rule: Rule
    tool: str
    call_index: int  # 1-based position among calls to this tool
    draw: float  # the seeded number compared with the rule's rate


def draw(seed: int, rule_id: str, tool: str, call_index: int) -> float:
    """A number in [0, 1) fixed by its inputs (no global random state)."""
    digest = hashlib.sha256(f"{seed}|{rule_id}|{tool}|{call_index}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


class Scheduler:
    """Decides, call by call, which rule (if any) injects a fault.

    Rules are tried in order and the first one that fires wins, so a call gets
    at most one fault.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self.calls: Counter[str] = Counter()
        self.injected: Counter[str] = Counter()

    def decide(self, tool: str) -> Decision | None:
        self.calls[tool] += 1
        n = self.calls[tool]
        for rule in self.config.rules:
            if not rule.matches(tool):
                continue
            if rule.max_count is not None and self.injected[rule.id] >= rule.max_count:
                continue
            d = draw(self.config.seed, rule.id, tool, n)
            if n in rule.nth or d < rule.rate:
                self.injected[rule.id] += 1
                return Decision(rule=rule, tool=tool, call_index=n, draw=d)
        return None


def parse_config(data: dict[str, Any] | None) -> Config:
    data = data or {}
    if not isinstance(data, dict):
        raise ConfigError("configuration must be a mapping")
    unknown = set(data) - {"seed", "rules", "log"}
    if unknown:
        raise ConfigError(f"unknown top-level keys: {sorted(unknown)}")
    seed = data.get("seed", 0)
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise ConfigError("seed must be an integer")
    raw_rules = data.get("rules") or []
    if not isinstance(raw_rules, list):
        raise ConfigError("rules must be a list")

    rules: list[Rule] = []
    seen: set[str] = set()
    for i, raw in enumerate(raw_rules):
        rules.append(_parse_rule(raw, i, seen))
    log = data.get("log")
    if log is not None and not isinstance(log, str):
        raise ConfigError("log must be a path")
    return Config(seed=seed, rules=tuple(rules), log=log)


def _parse_rule(raw: Any, index: int, seen: set[str]) -> Rule:
    where = f"rules[{index}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a mapping")
    rule_id = str(raw.get("id", f"rule-{index + 1}"))
    if rule_id in seen:
        raise ConfigError(f"{where}: duplicate rule id {rule_id!r}")
    seen.add(rule_id)
    where = f"rule {rule_id!r}"

    tool = raw.get("tool")
    fault = raw.get("fault")
    if not isinstance(tool, str) or not tool:
        raise ConfigError(f"{where}: 'tool' is required (a tool name or '*')")
    if not isinstance(fault, str) or not fault:
        raise ConfigError(f"{where}: 'fault' is required")

    rate = raw.get("rate", 0.0)
    if isinstance(rate, bool) or not isinstance(rate, int | float) or not 0.0 <= rate <= 1.0:
        raise ConfigError(f"{where}: 'rate' must be a number between 0 and 1")

    nth_raw = raw.get("nth", [])
    if isinstance(nth_raw, int) and not isinstance(nth_raw, bool):
        nth_raw = [nth_raw]
    if not isinstance(nth_raw, list) or not all(
        isinstance(n, int) and not isinstance(n, bool) and n >= 1 for n in nth_raw
    ):
        raise ConfigError(f"{where}: 'nth' must be a positive integer or a list of them")
    if rate == 0.0 and not nth_raw:
        raise ConfigError(f"{where}: set 'rate' and/or 'nth', or the rule never fires")

    max_count = raw.get("max_count")
    if max_count is not None and (
        isinstance(max_count, bool) or not isinstance(max_count, int) or max_count < 0
    ):
        raise ConfigError(f"{where}: 'max_count' must be a non-negative integer")

    tag = raw.get("tag")
    params = {k: v for k, v in raw.items() if k not in RULE_KEYS}
    return Rule(
        id=rule_id,
        tool=tool,
        fault=fault,
        rate=float(rate),
        nth=frozenset(nth_raw),
        max_count=max_count,
        tag=None if tag is None else str(tag),
        params=params,
    )


def load_config(path: str | Path) -> Config:
    with open(path, encoding="utf-8") as f:
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ConfigError(f"{path}: invalid YAML: {e}") from e
    return parse_config(data)
