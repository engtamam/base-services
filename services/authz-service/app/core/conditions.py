"""Safe structured-condition evaluator for ABAC.

A condition is JSON, NOT code:
    {}                                        -> always true
    {"all": [c1, c2, ...]}                    -> AND
    {"any": [c1, c2, ...]}                    -> OR
    {"not": c}                                -> NOT
    {"attr": "resource.owner", "op": "eq",    -> compare a context attribute
     "value": "$subject.id"}                     ($-values are context refs)

Evaluation is a pure interpreter over a flat context dict — there is NO eval and
no expression language, so a malicious policy or attribute value can never execute
code (defeats policy/expression injection, CWE-94/95). Unknown operators fail closed.
"""
from __future__ import annotations

from typing import Any

_MAX_DEPTH = 32


def _resolve_value(value: Any, ctx: dict[str, Any]) -> Any:
    if isinstance(value, str) and value.startswith("$"):
        return ctx.get(value[1:])
    return value


def _apply(op: str, left: Any, right: Any) -> bool:
    try:
        if op == "eq":
            return left == right
        if op == "ne":
            return left != right
        if op == "in":
            return left in right if right is not None else False
        if op == "nin":
            return left not in right if right is not None else True
        if op == "contains":
            return right in left if left is not None else False
        if op == "startswith":
            return isinstance(left, str) and isinstance(right, str) and left.startswith(right)
        if op == "exists":
            return left is not None
        if op in ("gt", "ge", "lt", "le"):
            if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
                return False
            return {"gt": left > right, "ge": left >= right,
                    "lt": left < right, "le": left <= right}[op]
    except TypeError:
        return False
    return False  # unknown operator -> fail closed


def evaluate(condition: dict[str, Any] | None, ctx: dict[str, Any], _depth: int = 0) -> bool:
    if not condition:
        return True
    if _depth > _MAX_DEPTH:
        return False
    if "all" in condition:
        return all(evaluate(c, ctx, _depth + 1) for c in condition["all"])
    if "any" in condition:
        return any(evaluate(c, ctx, _depth + 1) for c in condition["any"])
    if "not" in condition:
        return not evaluate(condition["not"], ctx, _depth + 1)
    if "attr" in condition and "op" in condition:
        left = ctx.get(condition["attr"])
        right = _resolve_value(condition.get("value"), ctx)
        return _apply(condition["op"], left, right)
    return False  # malformed condition -> fail closed
