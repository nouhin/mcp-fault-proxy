"""Configuration parsing and seeded, reproducible fault decisions."""

from __future__ import annotations

import pytest

from mcp_fault_proxy.config import ConfigError, Scheduler, draw, load_config, parse_config


def rule(**kw):
    base = {"id": "r", "tool": "t", "fault": "latency"}
    return {**base, **kw}


def run(config, calls):
    s = Scheduler(config)
    return [(d.rule.id, d.call_index) if (d := s.decide(tool)) else None for tool in calls]


def test_same_seed_gives_same_decisions():
    cfg = parse_config({"seed": 7, "rules": [rule(rate=0.3)]})
    calls = ["t"] * 200
    assert run(cfg, calls) == run(cfg, calls)


def test_different_seed_gives_different_decisions():
    calls = ["t"] * 200
    a = run(parse_config({"seed": 1, "rules": [rule(rate=0.3)]}), calls)
    b = run(parse_config({"seed": 2, "rules": [rule(rate=0.3)]}), calls)
    assert a != b


def test_decision_ignores_calls_to_other_tools():
    """Adding calls to an unrelated tool must not shift faults on this tool."""
    cfg = parse_config({"seed": 3, "rules": [rule(rate=0.4)]})
    alone = [d for d in run(cfg, ["t"] * 50) if d]
    mixed = [d for d in run(cfg, [x for _ in range(50) for x in ("other", "t")]) if d]
    assert alone == mixed


def test_rate_is_respected_roughly():
    cfg = parse_config({"seed": 11, "rules": [rule(rate=0.25)]})
    fired = sum(d is not None for d in run(cfg, ["t"] * 4000))
    assert 900 < fired < 1100


def test_nth_fires_exactly_on_those_calls():
    cfg = parse_config({"rules": [rule(nth=[2, 5])]})
    assert run(cfg, ["t"] * 6) == [None, ("r", 2), None, None, ("r", 5), None]


def test_max_count_caps_injections():
    cfg = parse_config({"rules": [rule(rate=1.0, max_count=2)]})
    assert run(cfg, ["t"] * 4) == [("r", 1), ("r", 2), None, None]


def test_first_matching_rule_wins_and_wildcard_matches_any_tool():
    cfg = parse_config(
        {"rules": [rule(id="a", nth=[1]), rule(id="b", tool="*", rate=1.0)]}
    )
    assert run(cfg, ["t", "t", "u"]) == [("a", 1), ("b", 2), ("b", 1)]


def test_extra_keys_become_fault_params():
    cfg = parse_config({"rules": [rule(rate=1.0, delay_ms=500, tag="L1")]})
    assert cfg.rules[0].params == {"delay_ms": 500}
    assert cfg.rules[0].tag == "L1"


def test_draw_is_in_unit_interval_and_stable():
    assert draw(0, "r", "t", 1) == draw(0, "r", "t", 1)
    assert 0.0 <= draw(0, "r", "t", 1) < 1.0


@pytest.mark.parametrize(
    "bad",
    [
        {"rules": [rule(rate=1.5)]},
        {"rules": [rule(rate=True)]},
        {"rules": [rule()]},  # never fires
        {"rules": [rule(nth=[0])]},
        {"rules": [rule(rate=1.0, max_count=-1)]},
        {"rules": [{"tool": "t", "rate": 1.0}]},  # no fault
        {"rules": [rule(rate=1.0), rule(rate=1.0)]},  # duplicate id
        {"seed": "x"},
        {"rulez": []},
    ],
)
def test_invalid_configs_are_rejected(bad):
    with pytest.raises(ConfigError):
        parse_config(bad)


def test_load_config_from_yaml(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("seed: 5\nlog: out.jsonl\nrules:\n  - {tool: t, fault: error, nth: 1}\n")
    cfg = load_config(p)
    assert cfg.seed == 5 and cfg.log == "out.jsonl" and cfg.rules[0].id == "rule-1"
