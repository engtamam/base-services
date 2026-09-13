"""Null identity lookup — no verification wired, so no verified-identity token is available.

Returns "" for every subject. A grant declared `once_per: verified_identity` then fails CLOSED
(withheld), because uniqueness cannot be established — a delayed gift is recoverable, a duplicated
one is not."""
from __future__ import annotations


class NullIdentityLookup:
    driver = "null"

    async def claim_ref_for(self, tenant: str, subject: str) -> str:
        return ""
