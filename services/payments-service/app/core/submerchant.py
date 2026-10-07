"""Sub-merchant mapping — `tenant -> provider destination id` (+ onboarding status).

In the marketplace model each tenant (shop) is a SUB-MERCHANT of the platform's provider account. The
provider does the KYC/KYB and holds the bank details; we persist ONLY the opaque **destination id** it
returns (plus the onboarding status), never the IBAN or any document — the minimum we need to route that
tenant's share of a split and nothing a leak could turn into a payout to the wrong account.

Keyed strictly per tenant (length-prefixed, so a tenant named "*" or crafted to collide cannot read or
overwrite another's mapping). A split for tenant T can only ever resolve T's own destination id.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

# Onboarding lifecycle, mirroring a provider's: pending (submitted, not yet approved) -> active
# (may receive payouts) | rejected. Only `active` is payable.
ST_NONE = "none"
ST_PENDING = "pending"
ST_ACTIVE = "active"
ST_REJECTED = "rejected"
_VALID = frozenset({ST_PENDING, ST_ACTIVE, ST_REJECTED})

_COLL = "submerchant_mappings"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class SubMerchantError(ValueError):
    """A sub-merchant mapping that cannot be stored safely."""


@dataclass(frozen=True)
class SubMerchant:
    tenant: str
    provider: str
    destination_id: str
    status: str
    set_at: str = ""


class SubMerchantStore:
    def __init__(self, store, *, now=_utc_now) -> None:
        self._s = store
        self._now = now

    @staticmethod
    def _key(tenant: str) -> str:
        # Length-prefixed (tenant-scoped-idempotency convention) so no tenant id can collide with
        # another's — a split/payout can never resolve a different tenant's destination.
        return f"{len(tenant)}:{tenant}|submerchant"

    async def set(self, *, tenant: str, provider: str, destination_id: str, status: str = ST_ACTIVE,
                  actor_ref: str = "") -> SubMerchant:
        """Record (or update) a tenant's sub-merchant destination. Refuses an empty tenant/destination
        and an unknown status — a payout target must be unambiguous."""
        if not tenant:
            raise SubMerchantError("a sub-merchant mapping requires a tenant")
        if not provider or not provider.strip():
            raise SubMerchantError("a sub-merchant mapping requires a provider")
        if not destination_id or not destination_id.strip():
            raise SubMerchantError("a sub-merchant mapping requires a non-empty destination_id")
        if status not in _VALID:
            raise SubMerchantError(f"unknown onboarding status {status!r}; valid: {sorted(_VALID)}")
        row = {"id": self._key(tenant), "tenant": tenant, "provider": provider.strip().lower(),
               "destination_id": destination_id.strip(), "status": status, "set_at": self._now()}
        # atomic: ok — single-writer (the onboarding route / webhook callback), last-write-wins is correct
        # for a mapping that the provider is the source of truth for.
        await self._s.put(_COLL, self._key(tenant), row)
        return self._state(row)

    async def get(self, tenant: str) -> SubMerchant | None:
        row = await self._s.get(_COLL, self._key(tenant))
        return self._state(row) if row else None

    async def destination_id(self, tenant: str) -> str:
        """The tenant's destination id, but ONLY when the mapping is ACTIVE — a pending/rejected
        sub-merchant must not receive a split (fail-closed). "" otherwise."""
        m = await self.get(tenant)
        return m.destination_id if (m and m.status == ST_ACTIVE) else ""

    async def status(self, tenant: str) -> str:
        m = await self.get(tenant)
        return m.status if m else ST_NONE

    async def clear(self, tenant: str) -> bool:
        return await self._s.delete(_COLL, self._key(tenant))

    @staticmethod
    def _state(row: dict) -> SubMerchant:
        return SubMerchant(tenant=row.get("tenant", ""), provider=row.get("provider", ""),
                           destination_id=row.get("destination_id", ""),
                           status=row.get("status", ST_NONE), set_at=row.get("set_at", ""))
