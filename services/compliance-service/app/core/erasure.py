"""Erasure — compliance-service: what it deletes for a data subject, and what it keeps, with the reason.

The consent RECORD is deleted — it is the person's current state and it goes with them.

The consent PROOF is not. It is the append-only evidence of what they agreed to and when,
and it is the only thing that answers a regulator asking 'prove they consented before you
processed'. Deleting it would destroy the defence for the processing that already happened.
The subject is pseudonymised in it instead. Basis: consent_proof.

A legal hold is retained by definition: a hold that an erasure could delete is not a hold.

Every ack names what was ERASED and what was RETAINED **with its legal basis**. "We kept it" is
always an answer with a reason attached, never a silence — a DSAR that cannot say why data
survived has not really answered the person.

Idempotent: a DSAR retries, so erasing an already-erased subject is a no-op that still acks.
"""
from __future__ import annotations

from typing import Any

from app.core.pii import subject_ref
from app.core.stores import ConsentStore, HoldStore


_CONSENT = "consent"
_PROOF = "consent_proof"
_HOLDS = "legal_holds"
_PROOF_SEQ = "consent_proof_seq"      # (tenant, RAW subject) -> counter; see stores.add_proof

RETENTION_BASIS = "consent_proof"

# Budget for the fail-CLOSED active-hold read. Completeness is what makes truncation SAFE: if a
# subject somehow has more active holds than this, the read is reported INCOMPLETE and the erase is
# refused, never proceeding on a page that might have hidden a hold.
_HOLD_SCAN_LIMIT = 1000
_PAGE = 500


class ComplianceErasure:
    service = "compliance-service"
    category = "consent"

    def __init__(self, *, store, pii_salt: str) -> None:
        self._store = store
        self._salt = pii_salt

    async def erase(self, tenant: str, subject: str) -> dict[str, Any]:
        ref = subject_ref(self._salt, tenant, subject)
        erased: list[str] = []

        # LEGAL HOLD is checked HERE, in the erasure itself — not only in the DsarService orchestrator.
        # This is the single seam every erase path funnels through, including the /internal/privacy/erase
        # route that calls it directly; enforcing the hold only upstream let a direct caller delete
        # held data. A hold an erasure could delete is not a hold: under an active hold this is a
        # no-op that reports WHY nothing was erased, and does NOT pseudonymise the proof either
        # (altering data under legal hold is itself a violation).
        #
        # FAIL CLOSED. We reuse HoldStore.active_for_subject, which pushes {"active": True} into the
        # store (never mixing released holds into the same read budget) AND returns whether the answer
        # is COMPLETE. A truncated hold read is treated EXACTLY like an active hold — refuse the erase —
        # because deleting data that MIGHT be under hold is spoliation. There is no `now`/expires_at
        # arithmetic: LegalHold has no expiry field, a hold ends only by being released (active=False),
        # and the store answers with the same `active` flag every other hold path uses. This also
        # removes the divergent wall clock the rest of the service never used.
        holds_repo = HoldStore(self._store)
        active_holds, holds_complete = await holds_repo.active_for_subject(
            tenant, subject, limit=_HOLD_SCAN_LIMIT)
        if active_holds or not holds_complete:
            why = ("an active legal hold blocks erasure — all subject data is retained unaltered "
                   "until the hold is released") if active_holds else (
                   "the legal-hold list could not be read completely — erasure is refused (fail "
                   "closed) rather than risk deleting data under an unread hold")
            return {
                "service": self.service,
                "subject_ref": ref,
                "erased": [],
                "retained": [{"what": f"legal_holds:{len(active_holds)}", "basis": "legal_hold",
                              "why": why}],
                "already_absent": False,
                "blocked_by_legal_hold": True,
                "hold_check_incomplete": not holds_complete,
            }

        # Delete under the CANONICAL key (row['id'], the normalised tenant::subject::purpose stamped at
        # write time) — NOT a raw-purpose reconstruction. The store key normalises the purpose
        # (casefold+strip), so f"...::{raw_purpose}" misses any purpose with uppercase/whitespace and
        # the row survived while the ack still claimed it erased — the exact false-completion this
        # service exists to prevent. Count only rows actually removed so `erased`/already_absent tell
        # the truth. (Fallback to the reconstructed key for a legacy row written before id-stamping.)
        #
        # PAGINATED to completion, exactly like the proof loop below. A single limit=1000 read left
        # any consent rows past the cap in place while the ack still reported success — the same
        # bounded-read false-completion the hold path just guarded against, one collection over.
        deleted = 0
        cursor = None
        while True:
            rows, cursor = await self._store.find(_CONSENT, {"tenant": tenant, "subject": subject},
                                                  limit=_PAGE, after=cursor, sort=("updated_at", "asc"))
            for r in rows:
                key = r.get("id") or f"{tenant}::{subject}::{ConsentStore.normalise(r.get('purpose', ''))}"
                if await self._store.delete(_CONSENT, key):
                    deleted += 1
            if not cursor:
                break
        if deleted:
            erased.append(f"consent:{deleted}")

        # the PROOF survives, pseudonymised — it is the evidence, not the person
        proofs = 0
        cursor = None
        while True:
            page, cursor = await self._store.find(_PROOF, {"tenant": tenant, "subject": subject},
                                                  limit=_PAGE, after=cursor, sort=("at", "asc"))
            for p in page:
                p["subject"] = ref
                await self._store.put(_PROOF, p["id"], p)
                proofs += 1
            if not cursor:
                break

        # The per-subject proof SEQUENCE counter is keyed "tenant::<RAW subject>" (stores.add_proof),
        # so it is itself a surviving raw-PII identifier — deleting the consent rows and pseudonymising
        # the proofs but leaving this row would let the erased person's raw id persist in the store and
        # in every sealed backup. Delete it: the counter is DERIVED and self-heals from the proof ids
        # (see internal._rebuild_proof_seq), so nothing depends on it surviving an erasure.
        await self._store.delete(_PROOF_SEQ, f"{tenant}::{subject}")

        retained = []
        if proofs:
            retained.append({"what": f"consent_proof:{proofs}", "basis": RETENTION_BASIS,
                             "why": "append-only evidence of what was agreed and when; subject pseudonymised"})

        return {
            "service": self.service,
            "subject_ref": ref,
            "erased": erased,
            "retained": retained,
            "already_absent": not erased,
        }
