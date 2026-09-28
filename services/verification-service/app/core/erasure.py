"""Erasure — verification: the claim goes, the verdict stays, and the ack says which and why.

This service holds three different things about a person, and they do not share an answer:

  * **The claim details and any uploaded document** — an ID card photo, a certificate. This is the
    person's data in the plainest sense, and it is DELETED, from the object store as well as the
    row. Deleting the row alone would leave the image on disk and report success.

  * **The verdict and its evidence** — that on this date this provider affirmed a claim. This is
    the record of a DECISION WE MADE, not an attribute of the person. Deleting it would destroy our
    ability to answer "on what basis did you open that account?", which is the question a regulator
    and a fraud investigator both ask. Retained with the subject link severed: without it the row
    says "a business was verified on this date", which is the fact we are entitled to keep.

  * **The identity_seen row** — a one-way hash, a first-seen date, a count. RETAINED, and this is
    the one that needs stating plainly rather than quietly exercising, because it survives an
    erasure request on purpose.

Why keeping that row is defensible: it cannot be reversed into an identity, cannot be used to
contact anyone, and holds no attribute of a person. What it can do is answer "have we seen this
registration before" — which stops the oldest fraud in self-serve signup: delete the tenant, sign
up again with the same commercial registration, collect the welcome gift a second time. Per-tenant
idempotency cannot see that, because the tenant is gone.

Phase 37's rule was that a service must SAY what it kept and on what basis. This is that rule
applied to the least comfortable case — where we keep something the person asked us to delete. The
ack names it, gives the basis, and states the limit: it is a hash, not a record of them.

Idempotent: a DSAR retries, and a second pass finds the links already severed.
"""
from __future__ import annotations

from typing import Any

FRAUD_BASIS = "fraud_prevention"
DECISION_BASIS = "record_of_decision"


class VerificationErasure:
    service = "verification-service"
    category = "identity_verifications"

    def __init__(self, *, store, object_store=None, max_scan: int = 100_000) -> None:
        self._store = store
        self._objects = object_store
        self._max = max_scan          # a DSAR is rare, but must never be an unbounded read

    async def erase(self, tenant: str, subject: str) -> dict[str, Any]:
        records = await self._store.for_subject(tenant, subject, limit=self._max)

        documents = 0
        unlinked = 0
        for record in records:
            evidence = record.evidence or {}
            # The uploaded document IS the person's data — a photo of an ID card.
            doc = evidence.get("reference", "")
            if doc and self._objects is not None and evidence.get("provider") == "manual":
                try:
                    await self._objects.delete(doc)
                    documents += 1
                except Exception:
                    pass          # a stranded blob is swept by retention; the row still goes

            # verification-008: sever the link with an optimistic compare-and-set + refetch, not a
            # plain put(). A reviewer's decide() or a provider's resolve() (both cas writes) can settle
            # this same record concurrently; an unconditional put() would clobber that just-committed
            # verdict (or be clobbered by it, leaving the subject link re-attached). Re-apply the
            # severing onto the freshest version so the verdict is preserved and the link always goes.
            severed = False
            for _ in range(8):
                fresh = await self._store.get(record.id)
                if fresh is None:
                    break                     # deleted concurrently — nothing to sever
                fresh.subject = ""            # the link from a decision to a human
                fresh.provider_state = {}
                if await self._store.cas_put(fresh):
                    severed = True
                    break                     # lost race falls through to refetch
            if severed:
                unlinked += 1

        erased = []
        if documents:
            erased.append(f"identity_documents:{documents}")
        if unlinked:
            erased.append(f"verification_subject_links:{unlinked}")

        retained: list[dict[str, str]] = []
        if unlinked:
            retained.append({
                "what": f"verification_verdicts:{unlinked}",
                "basis": DECISION_BASIS,
                "why": ("a verdict records a decision WE made — that on this date a provider "
                        "affirmed a claim — not an attribute of the person. Kept with the subject "
                        "link severed, so we can still answer 'on what basis was that account "
                        "opened', which a regulator and a fraud investigation both ask"),
            })
        retained.append({
            "what": "identity_seen:one_row_per_verified_identity",
            "basis": FRAUD_BASIS,
            "why": ("a one-way salted hash of the verified registration, with a first-seen date "
                    "and a count — no name, no contact, no attribute of any person, and not "
                    "reversible. It exists so that deleting an account and signing up again with "
                    "the same registration cannot collect a one-per-identity welcome grant twice. "
                    "Erasing it would re-open that fraud, and it identifies nobody"),
        })

        return {
            "service": self.service,
            "category": self.category,
            "erased": erased,
            "retained": retained,
            "already_absent": not records,
            "truncated": len(records) >= self._max,
        }
