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
from app.core.stores import ConsentStore


_CONSENT = "consent"
_PROOF = "consent_proof"
_HOLDS = "legal_holds"

RETENTION_BASIS = "consent_proof"


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
        import time as _time
        now = _time.time()
        all_holds, _ = await self._store.find(_HOLDS, {"tenant": tenant, "subject": subject},
                                              limit=100, sort=("placed_at", "asc"))
        active_holds = [h for h in all_holds
                        if h.get("active", True) and (not h.get("expires_at") or now < h["expires_at"])]
        if active_holds:
            return {
                "service": self.service,
                "subject_ref": ref,
                "erased": [],
                "retained": [{"what": f"legal_holds:{len(active_holds)}", "basis": "legal_hold",
                              "why": "an active legal hold blocks erasure — all subject data is "
                                     "retained unaltered until the hold is released"}],
                "already_absent": False,
                "blocked_by_legal_hold": True,
            }

        rows, _ = await self._store.find(_CONSENT, {"tenant": tenant, "subject": subject},
                                         limit=1000, sort=("updated_at", "asc"))
        # Delete under the CANONICAL key (row['id'], the normalised tenant::subject::purpose stamped at
        # write time) — NOT a raw-purpose reconstruction. The store key normalises the purpose
        # (casefold+strip), so f"...::{raw_purpose}" misses any purpose with uppercase/whitespace and
        # the row survived while the ack still claimed it erased — the exact false-completion this
        # service exists to prevent. Count only rows actually removed so `erased`/already_absent tell
        # the truth. (Fallback to the reconstructed key for a legacy row written before id-stamping.)
        deleted = 0
        for r in rows:
            key = r.get("id") or f"{tenant}::{subject}::{ConsentStore.normalise(r.get('purpose', ''))}"
            if await self._store.delete(_CONSENT, key):
                deleted += 1
        if deleted:
            erased.append(f"consent:{deleted}")

        # the PROOF survives, pseudonymised — it is the evidence, not the person
        proofs = 0
        cursor = None
        while True:
            page, cursor = await self._store.find(_PROOF, {"tenant": tenant, "subject": subject},
                                                  limit=500, after=cursor, sort=("at", "asc"))
            for p in page:
                p["subject"] = ref
                await self._store.put(_PROOF, p["id"], p)
                proofs += 1
            if not cursor:
                break

        retained = []
        if proofs:
            retained.append({"what": f"consent_proof:{proofs}", "basis": RETENTION_BASIS,
                             "why": "append-only evidence of what was agreed and when; subject pseudonymised"})
        if all_holds:
            # No ACTIVE hold reached here (an active one returns early above); these are released or
            # expired holds, retained as historical record.
            retained.append({"what": f"legal_holds:{len(all_holds)}", "basis": "legal_hold",
                             "why": "a hold an erasure could delete is not a hold"})

        return {
            "service": self.service,
            "subject_ref": ref,
            "erased": erased,
            "retained": retained,
            "already_absent": not erased,
        }
