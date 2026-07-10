"""Null entitlement checker — no billing wired, so entitlements never block a flag."""
from __future__ import annotations


class NullEntitlement:
    driver = "null"

    def allowed(self, *, tenant: str, key: str) -> bool:
        return True
