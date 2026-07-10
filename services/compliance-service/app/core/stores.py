"""Repositories over the generic DataStore. Consent proof is append-only."""
from __future__ import annotations

from typing import Any

from app.core.models import (Breach, Consent, ConsentProof, Dsar, DataTarget, LegalHold,
                             ProcessingActivity)
from app.ports.datastore import DataStore

_CONSENT = "consent"          # "<tenant>::<subject>::<purpose>"
_PROOF = "consent_proof"      # append-only
_DSAR = "dsar"
_HOLDS = "legal_holds"
_ROPA = "processing_activities"
_BREACH = "breaches"
_TARGETS = "data_targets"     # the data map
_SCAN = 1_000_000


class ConsentStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    @staticmethod
    def _key(tenant: str, subject: str, purpose: str) -> str:
        return f"{tenant}::{subject}::{purpose}"

    async def get(self, tenant: str, subject: str, purpose: str) -> Consent | None:
        row = await self._store.get(_CONSENT, self._key(tenant, subject, purpose))
        return Consent.from_dict(row) if row else None

    async def put(self, c: Consent) -> Consent:
        await self._store.put(_CONSENT, self._key(c.tenant, c.subject, c.purpose), c.to_dict())
        return c

    async def for_subject(self, tenant: str, subject: str, limit: int = 100) -> list[dict]:
        rows = [r for r in await self._store.list(_CONSENT, _SCAN)
                if r.get("tenant") == tenant and r.get("subject") == subject]
        return rows[:limit]

    # ── append-only proof ──────────────────────────────────────────────────────
    async def add_proof(self, p: ConsentProof) -> ConsentProof:
        await self._store.put(_PROOF, p.id, p.to_dict())
        return p

    async def proofs(self, tenant: str, subject: str, limit: int = 100) -> list[dict]:
        rows = [r for r in await self._store.list(_PROOF, _SCAN)
                if r.get("tenant") == tenant and r.get("subject") == subject]
        rows.sort(key=lambda r: r.get("at", 0))
        return rows[:limit]


class DsarStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def save(self, d: Dsar) -> Dsar:
        await self._store.put(_DSAR, d.id, d.to_dict())
        return d

    async def get(self, dsar_id: str) -> Dsar | None:
        row = await self._store.get(_DSAR, dsar_id)
        return Dsar.from_dict(row) if row else None

    async def open_for(self, tenant: str, subject: str, dsar_type: str) -> Dsar | None:
        """An identical open request → return it (idempotent, no double-processing)."""
        for r in await self._store.list(_DSAR, _SCAN):
            if (r.get("tenant") == tenant and r.get("subject") == subject
                    and r.get("type") == dsar_type
                    and r.get("status") not in ("completed", "refused")):
                return Dsar.from_dict(r)
        return None

    async def list(self, tenant: str, limit: int = 100) -> list[dict]:
        rows = [r for r in await self._store.list(_DSAR, _SCAN) if r.get("tenant") == tenant]
        rows.sort(key=lambda r: r.get("created_at", 0), reverse=True)
        return rows[:limit]


class HoldStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def save(self, h: LegalHold) -> LegalHold:
        await self._store.put(_HOLDS, h.id, h.to_dict())
        return h

    async def get(self, hold_id: str) -> LegalHold | None:
        row = await self._store.get(_HOLDS, hold_id)
        return LegalHold.from_dict(row) if row else None

    async def active_for(self, tenant: str, subject: str) -> list[LegalHold]:
        return [LegalHold.from_dict(r) for r in await self._store.list(_HOLDS, _SCAN)
                if r.get("tenant") == tenant and r.get("subject") == subject and r.get("active")]

    async def list(self, tenant: str, limit: int = 100) -> list[dict]:
        return [r for r in await self._store.list(_HOLDS, _SCAN) if r.get("tenant") == tenant][:limit]


class TargetStore:
    """The data map — which services hold which category of a subject's data."""

    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def register(self, t: DataTarget) -> DataTarget:
        await self._store.put(_TARGETS, t.id, t.to_dict())
        return t

    async def for_tenant(self, tenant: str) -> list[DataTarget]:
        return [DataTarget.from_dict(r) for r in await self._store.list(_TARGETS, _SCAN)
                if r.get("tenant") == tenant]

    async def delete(self, target_id: str) -> None:
        await self._store.delete(_TARGETS, target_id)


class RopaStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def save(self, a: ProcessingActivity) -> ProcessingActivity:
        await self._store.put(_ROPA, a.id, a.to_dict())
        return a

    async def get(self, activity_id: str) -> ProcessingActivity | None:
        row = await self._store.get(_ROPA, activity_id)
        return ProcessingActivity.from_dict(row) if row else None

    async def delete(self, activity_id: str) -> None:
        await self._store.delete(_ROPA, activity_id)

    async def list(self, tenant: str, limit: int = 100) -> list[dict]:
        return [r for r in await self._store.list(_ROPA, _SCAN) if r.get("tenant") == tenant][:limit]


class BreachStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def save(self, b: Breach) -> Breach:
        await self._store.put(_BREACH, b.id, b.to_dict())
        return b

    async def get(self, breach_id: str) -> Breach | None:
        row = await self._store.get(_BREACH, breach_id)
        return Breach.from_dict(row) if row else None

    async def list(self, tenant: str, limit: int = 100) -> list[dict]:
        rows = [r for r in await self._store.list(_BREACH, _SCAN) if r.get("tenant") == tenant]
        rows.sort(key=lambda r: r.get("discovered_at", 0), reverse=True)
        return rows[:limit]
