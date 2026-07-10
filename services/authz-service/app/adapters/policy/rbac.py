"""RBAC engine — allow if the subject holds the required capability (wildcards)."""
from __future__ import annotations

from typing import Any

from app.ports.policy_engine import Decision


def cap_matches(required: str, held: set[str], wildcard: bool) -> bool:
    if required in held or "*" in held:
        return True
    if wildcard:
        for cap in held:
            if cap.endswith(".*") and required.startswith(cap[:-1]):  # "files.*" -> "files."
                return True
    return False


class RbacEngine:
    driver = "rbac"

    def __init__(self, wildcard: bool = True) -> None:
        self._wildcard = wildcard

    def decide(self, ctx: dict[str, Any]) -> Decision:
        required = ctx.get("action", "")
        held = set(ctx.get("effective_caps") or [])
        if cap_matches(required, held, self._wildcard):
            return Decision(True, reason="rbac: capability granted")
        return Decision(False, reason=f"rbac: missing capability {required}")
