"""Repositories over the generic DataStore. Consent proof is append-only."""
from __future__ import annotations

from typing import Any

from app.core.models import (OPEN_DSAR_STATUSES, Breach, Consent, ConsentProof, Dsar,
                             DataTarget, LegalHold, ProcessingActivity)
from app.ports.datastore import DataStore

_CONSENT = "consent"          # "<tenant>::<subject>::<purpose>"
_PROOF = "consent_proof"      # append-only
_DSAR = "dsar"
_HOLDS = "legal_holds"
_ROPA = "processing_activities"
_BREACH = "breaches"
_TARGETS = "data_targets"     # the data map
_PROOF_SEQ = "consent_proof_seq"   # (tenant, subject) -> monotonic counter; see add_proof()
_SEQ_WIDTH = 12               # zero-padded so the id sorts as TEXT in both adapters

# Every read below is scoped to ONE tenant, and the scope is pushed into the datastore. It used
# to be applied in Python after reading up to a million rows of every tenant's consent records,
# DSARs and breach reports — the three collections a privacy regulator would ask about first.
#
# A note on ordering. The shipped btree index covers `created`; these collections order by
# `created_at` / `discovered_at` / `at`. The GIN containment still narrows to the tenant's own
# rows before the sort, so the cost is O(that tenant's records), not O(everyone's). It is not
# yet O(page). Where that matters, add the field to the adapter's `_INDEXES` — and match the
# COALESCE expression exactly, or Postgres will silently ignore the index.


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
        rows, _ = await self._store.find(_CONSENT, {"tenant": tenant, "subject": subject},
                                         limit=limit, sort=("updated_at", "desc"))
        return rows

    # ── append-only proof ──────────────────────────────────────────────────────
    async def add_proof(self, p: ConsentProof) -> ConsentProof:
        """Append a proof, stamping an id that sorts by INSERTION order.

        `proofs()` orders by `at`, and the datastore breaks ties on `id` to keep the order total
        — without a tie-break, two rows sharing a sort value can straddle a page boundary and one
        is silently lost. But a random uuid makes that tie-break ARBITRARY. A grant and a
        withdrawal recorded in the same clock tick then come back in random order, and the
        history handed to a regulator says the subject withdrew consent before they gave it.

        The old code never noticed because Python's sort is stable and the rows arrived in
        insertion order. Pushing the sort into the datastore removed that accident. So the id
        now carries a zero-padded per-subject counter: same tick, same `at`, still the order it
        was written in.
        """
        seq_key = f"{p.tenant}::{p.subject}"
        row = await self._store.get(_PROOF_SEQ, seq_key)
        seq = int((row or {}).get("n", 0)) + 1
        await self._store.put(_PROOF_SEQ, seq_key, {"n": seq})
        p.id = f"{seq:0{_SEQ_WIDTH}d}::{p.id}"
        await self._store.put(_PROOF, p.id, p.to_dict())
        return p

    async def proofs(self, tenant: str, subject: str, limit: int = 100) -> list[dict]:
        """The append-only consent proof for one subject, oldest first.

        This is the evidence produced when a regulator asks "prove they consented". Reading it
        used to mean reading every OTHER subject's proof too.
        """
        rows, _ = await self._store.find(_PROOF, {"tenant": tenant, "subject": subject},
                                         limit=limit, sort=("at", "asc"))
        return rows


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
        """An identical open request -> return it (idempotent, no double-processing).

        The equality filter cannot say "status NOT IN (...)", and inverting a negative into a
        positive list is how a new status silently becomes "open". So the OPEN statuses are
        named explicitly in the model and pushed down; anything not named is not open.
        """
        rows, _ = await self._store.find(
            _DSAR, {"tenant": tenant, "subject": subject, "type": dsar_type,
                    "status": list(OPEN_DSAR_STATUSES)},
            limit=1, sort=("created_at", "asc"))
        return Dsar.from_dict(rows[0]) if rows else None

    async def list(self, tenant: str, limit: int = 100, after: str | None = None) -> list[dict]:
        rows, _ = await self._store.find(_DSAR, {"tenant": tenant}, limit=limit, after=after,
                                         sort=("created_at", "desc"))
        return rows


class HoldStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def save(self, h: LegalHold) -> LegalHold:
        await self._store.put(_HOLDS, h.id, h.to_dict())
        return h

    async def get(self, hold_id: str) -> LegalHold | None:
        row = await self._store.get(_HOLDS, hold_id)
        return LegalHold.from_dict(row) if row else None

    async def active_for(self, tenant: str, subject: str, limit: int = 100) -> list[LegalHold]:
        """Holds that block an erasure. Read on every DSAR delete — a fail-CLOSED path.

        If this read is truncated the service concludes "no hold" and erases data it was
        legally required to keep. It is scoped, and the `active` flag is pushed down.
        """
        rows, _ = await self._store.find(
            _HOLDS, {"tenant": tenant, "subject": subject, "active": True},
            limit=limit, sort=("placed_at", "asc"))
        return [LegalHold.from_dict(r) for r in rows]

    async def list(self, tenant: str, limit: int = 100, after: str | None = None) -> list[dict]:
        rows, _ = await self._store.find(_HOLDS, {"tenant": tenant}, limit=limit, after=after,
                                         sort=("placed_at", "desc"))
        return rows


class TargetStore:
    """The data map — which services hold which category of a subject's data."""

    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def register(self, t: DataTarget) -> DataTarget:
        await self._store.put(_TARGETS, t.id, t.to_dict())
        return t

    async def for_tenant(self, tenant: str, limit: int = 1_000) -> list[DataTarget]:
        """The tenant's data map. Drives erasure fan-out: a missing target is data left behind."""
        rows, _ = await self._store.find(_TARGETS, {"tenant": tenant}, limit=limit,
                                         sort=("service", "asc"))
        return [DataTarget.from_dict(r) for r in rows]

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

    async def list(self, tenant: str, limit: int = 100, after: str | None = None) -> list[dict]:
        rows, _ = await self._store.find(_ROPA, {"tenant": tenant}, limit=limit, after=after,
                                         sort=("name", "asc"))
        return rows


class BreachStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def save(self, b: Breach) -> Breach:
        await self._store.put(_BREACH, b.id, b.to_dict())
        return b

    async def get(self, breach_id: str) -> Breach | None:
        row = await self._store.get(_BREACH, breach_id)
        return Breach.from_dict(row) if row else None

    async def list(self, tenant: str, limit: int = 100, after: str | None = None) -> list[dict]:
        rows, _ = await self._store.find(_BREACH, {"tenant": tenant}, limit=limit, after=after,
                                         sort=("discovered_at", "desc"))
        return rows
