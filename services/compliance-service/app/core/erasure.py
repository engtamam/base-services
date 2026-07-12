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

        rows, _ = await self._store.find(_CONSENT, {"tenant": tenant, "subject": subject},
                                         limit=1000, sort=("updated_at", "asc"))
        for r in rows:
            await self._store.delete(_CONSENT, f"{tenant}::{subject}::{r.get('purpose', '')}")
        if rows:
            erased.append(f"consent:{len(rows)}")

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

        holds, _ = await self._store.find(_HOLDS, {"tenant": tenant, "subject": subject},
                                          limit=100, sort=("placed_at", "asc"))

        retained = []
        if proofs:
            retained.append({"what": f"consent_proof:{proofs}", "basis": RETENTION_BASIS,
                             "why": "append-only evidence of what was agreed and when; subject pseudonymised"})
        if holds:
            retained.append({"what": f"legal_holds:{len(holds)}", "basis": "legal_hold",
                             "why": "a hold an erasure could delete is not a hold"})

        return {
            "service": self.service,
            "subject_ref": ref,
            "erased": erased,
            "retained": retained,
            "already_absent": not erased,
        }
