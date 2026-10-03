"""Manual review — a human looks at a document and decides.

This is the fallback that works everywhere. No provider contract, no API, no market-specific
integration: someone uploads an ID card or a commercial registration certificate and a reviewer
approves or refuses it. Every platform can run this on day one, which is why it is a first-class
adapter rather than a stopgap — a market we have no provider for is not a market where identity
stops mattering.

It is also the highest-privilege operation in the service. An automated verifier can only repeat
what a registry told it; a reviewer can grant trust to anyone. So:

**Approving is a critical auditable action.** Not because approvals are rare, but because a
disputed identity turns entirely on who approved it and what they were looking at. The evidence
records the reviewer and the document, so "we verified them" can be examined rather than asserted.

**A reviewer cannot approve their own claim.** Obvious, and precisely the check that gets left out
of a first version.

**Two-eyes is available and off by default.** Requiring two reviewers costs two staff; where one
person granting trust is unacceptable — high-value markets, regulated onboarding — it is one config
flag. Off by default because a platform with one operator would otherwise be unable to approve
anybody, and a control nobody can satisfy gets disabled entirely.
"""
from __future__ import annotations

from typing import Any

from app.core.models import Evidence, REFUSED, VERIFIED

MANUAL = "manual"


class ReviewRefused(ValueError):
    """The review cannot be recorded as given. Never downgraded to a warning."""


class ReviewNotFound(LookupError):
    """No such verification IN THE CALLER'S TENANT. A cross-tenant id reads identically to a missing
    one — the existence of another tenant's record is never leaked (a 404, not a 409)."""


class ReviewService:
    def __init__(self, *, store, clock, ttl_days: dict[str, int] | None = None,
                 two_eyes: bool = False, auditor=None) -> None:
        self._store = store
        self._clock = clock
        self._ttl = ttl_days or {}
        self._two_eyes = two_eyes
        self._auditor = auditor

    async def queue(self, tenant: str, limit: int = 100) -> list[dict[str, Any]]:
        """What is waiting for a human. Never exposes claim_ref or provider state."""
        rows = await self._store.pending_reviews(tenant, limit=limit)
        return [r.public() for r in rows]

    async def decide(self, verification_id: str, *, tenant: str, approve: bool, reviewer: str,
                     note: str = "", document_id: str = "") -> Any:
        """Record a reviewer's decision.

        `reviewer` is a pseudonym, like every other actor on this platform — the audit trail must
        say WHICH reviewer without the review queue becoming a staff directory.

        `tenant` is the CALLER's tenant and is enforced: a decision may only be made on a record in
        the caller's own tenant. Without this a reviewer holding `verification.review` in one tenant
        could approve/refuse another tenant's claim by id (a cross-tenant IDOR) — the review capability
        is tenant-scoped, so the record must be too. A cross-tenant or missing id is an identical 404.
        """
        # ATOMIC read-decide-write. The old path read the record, checked it was unsettled, then
        # put() with no version check: two concurrent decisions on one PENDING record both passed the
        # settled-check and both wrote (double-settle), and under two-eyes each concurrent first
        # approval read reviewers=[] and wrote its own single-element list, losing the other. Now every
        # write is a version compare-and-set; a lost race REFETCHES and re-validates against the new
        # state, so a settled record correctly refuses and a two-eyes tally accumulates.
        for _ in range(8):
            record = await self._store.get(verification_id)
            if record is None or record.tenant != tenant:
                raise ReviewNotFound("no such verification")
            if record.verdict in (VERIFIED, REFUSED):
                # Settled. Re-deciding would rewrite a decision someone already relied on; a change of
                # mind is a NEW verification, which keeps both decisions in the history.
                raise ReviewRefused(
                    f"this verification is already {record.verdict}; start a new one to re-check")
            if not reviewer:
                raise ReviewRefused("a review must name its reviewer")
            if reviewer == record.subject:
                # The check that gets left out of a first version.
                raise ReviewRefused("a reviewer cannot decide their own claim")

            now = self._clock.now()
            prior = list(record.provider_state.get("reviewers") or [])

            if self._two_eyes:
                # verification-013: two-eyes governs BOTH directions. A single reviewer refusing under a
                # two-eyes policy is just as unilateral as approving alone — a refusal is an appealable,
                # trust-affecting decision — so the same two-distinct-reviewer tally applies to refuse.
                # The control is TWO PEOPLE, not two clicks: a reviewer deciding twice does not satisfy it.
                if reviewer in prior or not prior:
                    if reviewer not in prior:
                        prior = prior + [reviewer]
                    # One signature is not a decision when the platform has said it needs two.
                    record.provider_state = {**record.provider_state, "reviewers": prior}
                    record.updated_at = now
                    if not await self._store.cas_put(record):
                        continue            # another reviewer wrote first — refetch and re-tally
                    return record
                # A second, DIFFERENT reviewer: the decision completes below.

            record.evidence = Evidence(provider=MANUAL, reference=document_id, checked_at=now,
                                       detail=note, reviewer=reviewer).to_dict()
            record.verdict = VERIFIED if approve else REFUSED
            record.expires_at = self._expiry_for(record.claim_type, now) if approve else 0.0
            record.pending_until = 0.0
            record.provider_state = {}
            record.updated_at = now
            # verification-004: WRITE-AHEAD the critical review.decided audit BEFORE the cas commit.
            # review.decided is declared critical (fail-closed), so a down audit log raises here — and
            # it must raise BEFORE the manual approval (the highest-privilege op in this service) is
            # persisted, never after. Both directions are audited: a human refusing someone is an
            # appealable decision, and the appeal needs a record of who made it. On a lost cas race the
            # record is refetched and re-decided (or refused), so the committed decision is always the
            # one that was audited immediately before it.
            if self._auditor is not None:
                await self._auditor.record(
                    "review.decided", actor=reviewer, tenant=record.tenant,
                    resource=f"{record.claim_type}:{record.id}",
                    metadata={"approved": approve, "document": bool(document_id)})
            if not await self._store.cas_put(record):
                continue                    # lost the race — refetch; the winner may have settled it
            return record
        raise ReviewRefused("this verification is being decided concurrently; retry")

    def _expiry_for(self, claim_type: str, now: float) -> float:
        days = self._ttl.get(claim_type, 0)
        return now + days * 86400 if days > 0 else 0.0
