"""Repositories over the generic DataStore.

Two collections, and the split is the whole anti-abuse design:

    verifications   one row per check. Subject data — erasable.
    identity_seen   one row per distinct identity ever verified. A one-way hash and nothing else.
                    NOT erasable, and that is a decision with a reason, stated in the erasure ack.

Keeping the second is what stops the oldest trick in the book: delete the tenant, sign up again
with the same commercial registration, collect the welcome gift a second time. Per-tenant
idempotency cannot see that — the tenant is gone. Both reference implementations we studied got
this wrong in exactly that way.

It is defensible to keep because the row names nobody: a salted one-way hash, a first-seen
timestamp, and a count. It cannot be reversed into an identity, cannot be used to contact anyone,
and holds no attribute of a person. What it can do is answer "have we seen this registration
before", which is fraud prevention — a recognised basis for retention, and one the erasure ack
states out loud rather than quietly exercising.
"""
from __future__ import annotations

from typing import Any

from app.core.models import PENDING, Verification

_VERIFICATIONS = "verifications"
_SEEN = "identity_seen"


class VerificationStore:
    def __init__(self, store) -> None:
        self._store = store

    async def put(self, record: Verification) -> Verification:
        await self._store.put(_VERIFICATIONS, record.id, record.to_dict())
        return record

    async def cas_put(self, record: Verification) -> bool:
        """Persist `record` ONLY if the stored row is still at `record.version`. Returns True and
        advances `record.version` on success, False if another writer moved it first. This is the
        atomic seam a manual decision needs: two concurrent reviews of one PENDING record can no
        longer both pass the settled-check and both write, losing a two-eyes tally or double-settling.
        """
        ok = await self._store.cas(_VERIFICATIONS, record.id, record.version, record.to_dict())
        if ok:
            record.version += 1
        return ok

    async def get(self, verification_id: str) -> Verification | None:
        row = await self._store.get(_VERIFICATIONS, verification_id)
        return Verification.from_dict(row) if row else None

    async def latest(self, tenant: str, subject: str, claim_type: str) -> Verification | None:
        """The most recent check of this claim. Sorted newest-first so a re-verification
        supersedes the old verdict without deleting it — the history IS the audit trail."""
        rows, _ = await self._store.find(
            _VERIFICATIONS, {"tenant": tenant, "subject": subject, "claim_type": claim_type},
            limit=1, sort=("created_at", "desc"))
        return Verification.from_dict(rows[0]) if rows else None

    async def for_subject(self, tenant: str, subject: str, limit: int = 100) -> list[Verification]:
        rows, _ = await self._store.find(_VERIFICATIONS, {"tenant": tenant, "subject": subject},
                                         limit=limit, sort=("created_at", "desc"))
        return [Verification.from_dict(r) for r in rows]

    async def pending_before(self, cutoff: float, limit: int = 500) -> list[Verification]:
        """Prompts that have lapsed. Filtered on the explicit PENDING status rather than on
        'not settled', so a verdict added later is not swept by accident."""
        rows, _ = await self._store.find(_VERIFICATIONS, {"verdict": PENDING}, limit=limit,
                                         sort=("created_at", "asc"))
        return [r for r in (Verification.from_dict(row) for row in rows)
                if r.pending_until and cutoff >= r.pending_until]

    async def pending_reviews(self, tenant: str, limit: int = 100) -> list[Verification]:
        """Claims waiting on a human. Oldest first — a queue that serves newest-first leaves the
        person who has waited longest waiting longest."""
        rows, _ = await self._store.find(_VERIFICATIONS, {"tenant": tenant, "verdict": PENDING},
                                         limit=limit, sort=("created_at", "asc"))
        return [Verification.from_dict(r) for r in rows]

    async def delete(self, verification_id: str) -> None:
        await self._store.delete(_VERIFICATIONS, verification_id)


class IdentityRegistry:
    """Which distinct identities we have ever verified. Survives erasure by design."""

    def __init__(self, store) -> None:
        self._store = store

    async def remember(self, claim_ref: str, now: float, *, _max_retries: int = 8) -> dict[str, Any]:
        """Record that this identity has been verified, and say whether we had seen it before.

        The return value is what an onboarding grant checks: `first_seen` False means this
        registration has already collected whatever is given once per identity.

        ATOMIC: the old get-then-put let two concurrent verifications of the SAME identity both read
        "absent" and both write a fresh row with first_seen=True/times=1 — one clobbering the other,
        so a repeat registration reads as first-seen and the once-per-identity grant fires twice. Now
        the first writer wins an insert-if-absent (cas expected_version=0) and every other writer
        takes the increment path under a version-pinned cas, retrying on a lost race so the `times`
        count is never lost.
        """
        for _ in range(max(1, _max_retries)):
            existing = await self._store.get(_SEEN, claim_ref)
            if existing:
                updated = dict(existing)
                updated["times"] = int(existing.get("times", 1)) + 1
                updated["last_at"] = now
                if await self._store.cas(_SEEN, claim_ref, int(existing.get("version", 0)), updated):
                    return {"first_seen": False, "record": updated}
                continue                    # another writer moved first — refetch and re-increment
            row = {"id": claim_ref, "first_at": now, "last_at": now, "times": 1}
            if await self._store.cas(_SEEN, claim_ref, 0, row):
                return {"first_seen": True, "record": row}
            continue                        # someone inserted between our get and cas — count them
        # Extremely contended identity: fall back to a best-effort read so we never spin forever.
        existing = await self._store.get(_SEEN, claim_ref)
        return {"first_seen": existing is None, "record": existing or {"id": claim_ref}}

    async def seen(self, claim_ref: str) -> bool:
        return await self._store.get(_SEEN, claim_ref) is not None
