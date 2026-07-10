"""Declarative field mapping — data shape translation without code.

A mapping is JSON, never an expression:
    {"target_field": {"op": "path",  "value": "properties.email"},
     "plan":         {"op": "const", "value": "pro"},
     "name":         {"op": "path",  "value": "firstname", "default": "unknown"},
     "id":           {"op": "rename","value": "vid"}}

There is **no eval and no expression language**, so a malicious mapping (or a hostile field
value) can never execute code — the same stance as the authz condition evaluator and the
search query DSL. An unknown op fails closed (the field is dropped, never guessed).
"""
from __future__ import annotations

from typing import Any

_OPS = ("path", "rename", "const", "concat")


class MappingError(ValueError):
    pass


def get_path(record: dict, dotted: str) -> Any:
    """`a.b.c` — traverse dicts only. A missing hop yields None, never an exception."""
    cur: Any = record
    for part in str(dotted).split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def validate(mapping: dict[str, Any]) -> None:
    if not isinstance(mapping, dict):
        raise MappingError("MAPPING_MUST_BE_OBJECT")
    for field, rule in mapping.items():
        if not isinstance(rule, dict) or rule.get("op") not in _OPS:
            raise MappingError(f"BAD_OP_FOR_FIELD:{field}")
        if rule["op"] == "concat" and not isinstance(rule.get("value"), list):
            raise MappingError(f"CONCAT_NEEDS_LIST:{field}")


def apply(mapping: dict[str, Any], record: dict) -> dict:
    """Translate one source record into the target shape. Unknown ops are impossible here
    (validate() gates them); a missing source value falls back to `default`, else is dropped."""
    out: dict[str, Any] = {}
    for field, rule in (mapping or {}).items():
        op = rule.get("op")
        if op == "const":
            out[field] = rule.get("value")
            continue
        if op == "concat":
            parts = [str(get_path(record, p) or "") for p in rule.get("value", [])]
            joined = (rule.get("sep", " ")).join(p for p in parts if p)
            if joined:
                out[field] = joined
            continue
        if op in ("path", "rename"):
            val = get_path(record, rule.get("value", ""))
            if val is None:
                if "default" in rule:
                    out[field] = rule["default"]
                continue                       # otherwise drop — never invent data
            out[field] = val
    return out
