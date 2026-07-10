"""Targeting rules — safe attribute matchers. No eval, no user-supplied regex (ReDoS-safe).

A rule: {"attribute": "country", "op": "in", "value": ["SA","AE"], "enabled": true}
All conditions of a rule must match (AND). Unknown op / missing attribute → no match.
"""
from __future__ import annotations

from typing import Any

_OPS = ("equals", "not_equals", "in", "not_in", "contains", "prefix", "suffix", "gt", "gte", "lt", "lte")


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def match_condition(cond: dict[str, Any], attrs: dict[str, Any]) -> bool:
    attr, op, expected = cond.get("attribute"), cond.get("op"), cond.get("value")
    if attr not in attrs or op not in _OPS:
        return False                                   # fail-closed: unknown attr/op never matches
    actual = attrs[attr]
    if op == "equals":
        return actual == expected
    if op == "not_equals":
        return actual != expected
    if op == "in":
        return isinstance(expected, (list, tuple)) and actual in expected
    if op == "not_in":
        return isinstance(expected, (list, tuple)) and actual not in expected
    if op == "contains":
        return isinstance(actual, str) and isinstance(expected, str) and expected in actual
    if op == "prefix":
        return isinstance(actual, str) and isinstance(expected, str) and actual.startswith(expected)
    if op == "suffix":
        return isinstance(actual, str) and isinstance(expected, str) and actual.endswith(expected)
    a, e = _num(actual), _num(expected)
    if a is None or e is None:
        return False
    return {"gt": a > e, "gte": a >= e, "lt": a < e, "lte": a <= e}[op]


def match_rule(rule: dict[str, Any], attrs: dict[str, Any]) -> bool:
    conds = rule.get("conditions") or []
    return bool(conds) and all(match_condition(c, attrs) for c in conds)


def first_match(rules: list[dict[str, Any]], attrs: dict[str, Any]) -> dict[str, Any] | None:
    for rule in rules or []:
        if match_rule(rule, attrs):
            return rule
    return None
