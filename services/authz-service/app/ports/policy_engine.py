"""PolicyEngine port — the decision function (RBAC / ABAC / combined)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str = ""
    matched_policy: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"allow": self.allow, "reason": self.reason, "matched_policy": self.matched_policy}


@runtime_checkable
class PolicyEngine(Protocol):
    driver: str

    def decide(self, ctx: dict[str, Any]) -> Decision:
        """ctx = {subject, action, resource, effective_caps, attributes, policies}."""
        ...
