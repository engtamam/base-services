"""The disabled legal gate: nobody ever owes anything.

Wired when IDENTITY_LEGAL_ENFORCEMENT=off (the default), so every mint path behaves exactly as it
did before legal enforcement existed — no compliance dependency, no network call, no change.
"""
from __future__ import annotations


class NoLegalGate:
    driver = "off"

    async def outstanding(self, *, subject: str, tenant: str) -> list[dict]:
        return []
