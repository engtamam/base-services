"""Scoped ticket visibility — a shop's CONSENT to let exactly ONE named outside party read a scoped
subset of its tickets, and nothing more.

This is the primitive between two capabilities that were both wrong for the job:

  * `tickets.self`     — the raiser's own tickets. Too narrow: an outside party is not the raiser.
  * `platform.tickets` — every tenant's tickets, for an operator. Far too broad: a marketer helping
    one shop must never gain a cross-tenant reach over all of them.

A `ShareGrant` is the middle a shop actually consents to (spec §13): (tenant, grantee) with a scope.
`grantee` is a STABLE, OPAQUE reference to the one outside party — a pseudonym or an audience id, never
a raw identity — so a grant names a party without storing who they are. The scope narrows WHICH tickets
(by type / status); an empty scope means all of the tenant's tickets. A grant is revocable and may
expire, because consent is withdrawable and time-boxed by nature.

The grant is the ENFORCEMENT point, held by ticket-service itself — not a promise a calling peer makes.
A peer presents (tenant, grantee); this service decides what, if anything, that grantee may see. One
grant per (tenant, grantee): the id is derived from the pair, so recording consent twice is one upsert,
not two rows that can disagree.

GENERAL, not affiliate-shaped: any platform that must let a named third party (a partner, an auditor, a
reseller) see a consented, scoped slice of one tenant's tickets uses the same grant — the grantee is
whoever the platform names.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_COLLECTION = "ticket_shares"


def grant_id(tenant: str, grantee: str) -> str:
    """One grant per (tenant, grantee). Length-prefixed so no (tenant, grantee) pair can collide with
    another by moving the separator — the same discipline tenant-scoped idempotency keys use."""
    return f"{len(tenant)}:{tenant}:{grantee}"


@dataclass
class ShareGrant:
    tenant: str
    grantee: str
    scope: dict[str, Any] = field(default_factory=dict)   # {"type": [...], "status": [...]}; {} = all
    # What the grantee may DO within the scope. "read" is implied by any grant; "write" additionally
    # lets the grantee OPEN and REPLY on the tenant's behalf. Read-only by default, so an existing grant
    # and a cautious platform stay read-only unless write is explicitly consented to — a platform that
    # wants a partner to act (not just watch) adds "write"; one sharing with an auditor never does.
    permissions: list[str] = field(default_factory=lambda: ["read"])
    granted_by: str = ""                                  # pseudonym of who consented (the shop)
    granted_at: str = ""
    expires_at: float = 0.0                               # epoch seconds; 0 = never
    revoked: bool = False

    @property
    def id(self) -> str:
        return grant_id(self.tenant, self.grantee)

    @property
    def can_write(self) -> bool:
        return "write" in (self.permissions or [])

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "tenant": self.tenant, "grantee": self.grantee,
                "scope": dict(self.scope or {}), "permissions": list(self.permissions or ["read"]),
                "granted_by": self.granted_by, "granted_at": self.granted_at,
                "expires_at": float(self.expires_at or 0.0), "revoked": bool(self.revoked)}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ShareGrant":
        return cls(tenant=d.get("tenant", ""), grantee=d.get("grantee", ""),
                   scope=dict(d.get("scope") or {}),
                   permissions=list(d.get("permissions") or ["read"]),
                   granted_by=d.get("granted_by", ""), granted_at=d.get("granted_at", ""),
                   expires_at=float(d.get("expires_at") or 0.0), revoked=bool(d.get("revoked", False)))

    def is_active(self, now: float) -> bool:
        """A grant a peer may act on: consent given, not withdrawn, not lapsed. Fails CLOSED — an
        expired or revoked grant grants nothing, the same posture entitlement resolution takes."""
        if self.revoked:
            return False
        return not self.expires_at or now < self.expires_at

    def covers(self, ticket: dict[str, Any]) -> bool:
        """Does this grant's scope reach this ticket? An empty list for a dimension means 'any'; a
        non-empty list is an allowlist. Deny is never expressible by omission — only a present,
        non-matching value narrows, so widening a grant is always an explicit act."""
        for key in ("type", "status"):
            allowed = self.scope.get(key)
            if allowed and ticket.get(key) not in allowed:
                return False
        return True


class ShareStore:
    collection = _COLLECTION

    def __init__(self, store) -> None:
        self._s = store

    async def upsert(self, grant: ShareGrant) -> ShareGrant:
        await self._s.put(self.collection, grant.id, grant.to_dict())
        return grant

    async def get(self, tenant: str, grantee: str) -> ShareGrant | None:
        row = await self._s.get(self.collection, grant_id(tenant, grantee))
        return ShareGrant.from_dict(row) if row else None

    async def revoke(self, tenant: str, grantee: str) -> bool:
        """Withdraw consent. Idempotent: revoking an absent or already-revoked grant is success, so a
        shop clicking twice — or a retry — never errors. Kept as a tombstone (revoked=True) rather than
        deleted, so 'consent was withdrawn' stays an answerable fact, not a silence."""
        g = await self.get(tenant, grantee)
        if g is None or g.revoked:
            return False
        g.revoked = True
        await self._s.put(self.collection, g.id, g.to_dict())
        return True

    async def list_for_tenant(self, tenant: str, limit: int = 200) -> list[dict[str, Any]]:
        """Every party a tenant has shared with — a FILTERED find (never a scan), so a shop can see
        and audit exactly who it has consented to."""
        rows, _ = await self._s.find(self.collection, {"tenant": tenant}, limit=limit)
        return rows
