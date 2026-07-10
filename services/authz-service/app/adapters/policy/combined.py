"""Combined engine — ABAC explicit deny wins; else allow if RBAC or ABAC allows."""
from __future__ import annotations

from typing import Any

from app.ports.policy_engine import Decision


class CombinedEngine:
    driver = "combined"

    def __init__(self, rbac, abac) -> None:
        self._rbac = rbac
        self._abac = abac

    def decide(self, ctx: dict[str, Any]) -> Decision:
        ad = self._abac.decide(ctx)
        if ad.matched_policy is not None and not ad.allow:
            return ad  # explicit deny overrides everything
        rd = self._rbac.decide(ctx)
        if rd.allow:
            return rd
        if ad.allow:
            return ad
        return Decision(False, reason="combined: no capability and no allow policy")
