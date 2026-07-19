"""Erasure — the contract every service in this platform answers.

A new service is born connected to the DSAR fan-out because this file exists. Fill in the two
methods and it is done; delete this file and the service silently becomes a place personal data
goes to hide.

WRITE THIS FILE HONESTLY. It is the one piece of code a regulator would read line by line, and the
three rules below are what separate a compliant answer from a plausible one:

  1. **Never report success for work that did not happen.** If you cannot reach the rows, cannot
     identify the subject, or must keep the data, say so. A false "she was erased" is the worst
     thing this platform can say. `already_absent` means you LOOKED and found nothing — it is not
     a default.

  2. **Say what you KEPT, and why, with a basis.** "We kept it" is always an answer with a reason
     attached, never a silence. A tax invoice, an audit chain, a legal hold, a consent proof —
     these survive erasure, and the person is entitled to be told which law keeps them.

  3. **Decide delete vs pseudonymise vs redact deliberately.** Look at how the platform's other
     services chose, because the same walker leads to different answers:
        billing   pseudonymises — the invoice must survive, the buyer's name must not
        zatca     retains everything — a signed, hash-chained invoice cannot even be altered
        webhook   redacts and keeps the row — the delivery history is operational record
        connector deletes the row — a DLQ entry gets RETRIED, so a redacted one corrupts a CRM
        job       redacts AND cancels — pending work would still contact the erased person
        stream    refuses without identification — guessing would delete other people's video

  4. **Be idempotent.** A DSAR retries. Erasing an already-erased subject is a no-op that still
     returns a receipt; it must never fail the second time.

See docs/contracts/privacy-erase.schema.json for the wire format and
docs/contracts/privacy-erase-guide.md for the three steps to connect a service.
"""
from __future__ import annotations

from typing import Any

from app.core.pii import subject_ref


class OnboardingErasure:
    # Both appear in the DSAR's target list, so they must match the data-map row registered for
    # this service. A mismatch means the DSAR waits for an ack that never arrives.
    service = "onboarding-service"
    category = "onboarding_progress"

    def __init__(self, *, store, pii_salt: str, max_scan: int = 100_000) -> None:
        self._store = store
        self._salt = pii_salt
        self._max = max_scan          # a DSAR is rare, but must never be an unbounded read

    async def erase(self, tenant: str, subject: str) -> dict[str, Any]:
        ref = subject_ref(self._salt, tenant, subject)

        # ── TODO: erase this service's subject data ──────────────────────────
        # Scope EVERY query to `tenant`: one customer's DSAR must never touch another's rows.
        # Page with a cursor and stop at `self._max` rather than reading an unbounded set.
        erased: list[str] = []
        retained: list[dict[str, str]] = []

        # Progress and operator overrides are this person's data and go.
        for collection in ("onboarding_forced", "onboarding_grants"):
            rows, _ = await self._store.find(collection, {"tenant": tenant, "subject": subject},
                                             limit=self._max, sort=("id", "asc"))
            for row in rows:
                await self._store.delete(collection, row["id"])
            if rows:
                erased.append(f"{collection}:{len(rows)}")

        # The grant ledger does NOT go, and this is the uncomfortable one — same shape as
        # verification-service's identity_seen, and kept for the same reason.
        retained.append({
            "what": "grant_ledger:one_row_per_issued_grant",
            "basis": "fraud_prevention",
            "why": ("a one-way key derived from a VERIFIED identity, with the grant kind and a "
                    "date — no name, no subject id, no contact, not reversible. It exists so that "
                    "deleting an account and signing up again with the same registration cannot "
                    "collect a one-per-identity welcome grant twice. Erasing it would re-open "
                    "exactly that fraud, and the row identifies nobody"),
        })

        # Example of a retention that is legally required — copy the SHAPE, not the words:
        # retained.append({
        #     "what": "invoices:12",
        #     "basis": "tax_retention",
        #     "why": "a tax invoice must survive ~10 years by law; the buyer's name is "
        #            "pseudonymised and the financial record is untouched",
        # })

        return {
            "service": self.service,
            "category": self.category,
            "subject_ref": ref,          # the pseudonym — never echo the raw subject back
            "erased": erased,
            "retained": retained,
            # Only true when you looked and there was nothing. Never a default, and never a way to
            # avoid admitting that a lookup failed.
            "already_absent": not erased and not retained,
        }
