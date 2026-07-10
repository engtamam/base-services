"""ABAC engine — evaluate attribute policies (deny-overrides by default).

A policy applies when its action/resource globs match AND its condition evaluates
true against the request context. Deny-overrides: any applying deny wins; else any
applying allow grants; else default deny.
"""
from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Any

from app.core.conditions import evaluate
from app.ports.policy_engine import Decision


class AbacEngine:
    driver = "abac"

    def __init__(self, deny_overrides: bool = True) -> None:
        self._deny_overrides = deny_overrides

    def decide(self, ctx: dict[str, Any]) -> Decision:
        action = ctx.get("action", "")
        resource = ctx.get("resource", "")
        eval_ctx = self._context(ctx)
        allow_match: str | None = None
        deny_match: str | None = None
        for p in ctx.get("policies") or []:
            if not fnmatchcase(action, p.get("action", "*")):
                continue
            if not fnmatchcase(resource, p.get("resource", "*")):
                continue
            if not evaluate(p.get("condition") or {}, eval_ctx):
                continue
            if p.get("effect") == "deny":
                deny_match = p.get("id", "deny")
            elif p.get("effect") == "allow":
                allow_match = allow_match or p.get("id", "allow")
        if self._deny_overrides and deny_match is not None:
            return Decision(False, reason="abac: explicit deny", matched_policy=deny_match)
        if allow_match is not None:
            return Decision(True, reason="abac: allow policy", matched_policy=allow_match)
        if deny_match is not None:
            return Decision(False, reason="abac: explicit deny", matched_policy=deny_match)
        return Decision(False, reason="abac: no matching allow policy")

    @staticmethod
    def _context(ctx: dict[str, Any]) -> dict[str, Any]:
        # Flat attribute context: caller-supplied attributes + a few standard keys.
        base = dict(ctx.get("attributes") or {})
        base.setdefault("subject.id", ctx.get("subject", ""))
        base.setdefault("action", ctx.get("action", ""))
        base.setdefault("resource.id", ctx.get("resource", ""))
        return base
