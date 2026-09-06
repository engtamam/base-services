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


class TemplateErasure:
    # Both appear in the DSAR's target list, so they must match the data-map row registered for
    # this service. A mismatch means the DSAR waits for an ack that never arrives — hence these are
    # the REAL identifiers, matching manifest.py's /internal/privacy/erase erasure_category="subject".
    service = "assistant-service"
    category = "subject"

    def __init__(self, *, store, pii_salt: str, conversations=None,
                 max_scan: int = 100_000) -> None:
        self._store = store
        self._salt = pii_salt
        # This service's ONE store of subject personal data: the conversations (and their messages).
        # It carries subject_ref on every row and already exposes a tenant-scoped, cursor-paged
        # erase_subject; the DSAR simply drives it. None only in a deployment that keeps no
        # conversations, where there is genuinely nothing to erase.
        self._conversations = conversations
        self._max = max_scan          # a DSAR is rare, but must never be an unbounded read

    async def erase(self, tenant: str, subject: str) -> dict[str, Any]:
        ref = subject_ref(self._salt, tenant, subject)

        # Conversations are this service's subject data. erase_subject deletes messages before their
        # conversation, scoped to (tenant, subject_ref), paged — so a DSAR removes everything this
        # person asked. Reported by COUNT, never by content or raw id.
        erased: list[str] = []
        retained: list[dict[str, str]] = []
        if self._conversations is not None:
            removed = await self._conversations.erase_subject(tenant=tenant, subject=subject)
            if removed.get("conversations"):
                erased.append(f"conversations:{removed['conversations']}")
            if removed.get("messages"):
                erased.append(f"messages:{removed['messages']}")
        # Nothing is RETAINED: an assistant conversation carries no tax/legal-hold basis to keep it,
        # so this list stays empty by design. (The pseudonymous attribution audit line is NOT subject
        # data kept here — it is an append-only record carrying no content, exactly so it can survive.)

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
