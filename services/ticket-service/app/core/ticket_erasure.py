"""Erase a data subject from tickets — the answer to a DSAR fan-out.

A ticket holds PII: the free text a person wrote (the subject line and the message bodies). When a
DSAR erases that person, this REDACTS that free text while KEEPING the ticket's shape and history —
its type, status, timeline, and the pseudonymous `raiser_ref`. A resolved dispute must stay provable
("a ticket of this kind was raised and closed on this date") without holding what the person wrote.

The pseudonym itself is not deleted here: `raiser_ref` is already an HMAC with no inverse, and the
LINKAGE (pseudonym -> person) is destroyed in identity-service. Once that linkage is gone, every
`raiser_ref` here is an orphan that names nobody — the same T2 pattern audit and compliance use.

Idempotent: a DSAR retries, so erasing an already-erased subject re-redacts nothing and succeeds.
Bounded: reads are filtered by `(tenant, raiser_ref)` and capped at `max_scan` — a DSAR is rare, but
must never become an unbounded read.
"""
from __future__ import annotations

from typing import Any

from app.core.pii import subject_ref

_REDACTED = "[erased]"


class TicketErasure:
    service = "ticket-service"

    def __init__(self, *, store, pii_salt: str, max_scan: int = 100_000) -> None:
        self._s = store
        self._salt = pii_salt
        self._max = max_scan

    async def erase(self, tenant: str, subject: str) -> dict[str, Any]:
        ref = subject_ref(self._salt, tenant, subject)
        erased: list[str] = []
        retained: list[dict[str, str]] = []

        # The tickets this subject RAISED — redact the free text, keep the record.
        tickets, _ = await self._s.find("tickets", {"tenant": tenant, "raiser_ref": ref},
                                        limit=self._max, sort=("created", "asc"))
        for row in tickets:
            if row.get("subject") != _REDACTED:
                row["subject"] = _REDACTED
                row["meta"] = {}            # meta may echo submitted content
                await self._s.put("tickets", row["id"], row)
        if tickets:
            erased.append(f"ticket_subjects:{len(tickets)}")
            retained.append({
                "what": "ticket_records",
                "basis": "operational_record",
                "why": "the ticket's type, status and timeline are kept as a pseudonymous record so "
                       "a resolved request stays provable; they hold no free text after redaction"})

        # The events this subject AUTHORED — their opening message and their own replies. Operator
        # replies are a different author and are left as the service's own record.
        events, _ = await self._s.find("ticket_events", {"tenant": tenant, "actor_ref": ref},
                                       limit=self._max, sort=("created", "asc"))
        redacted = 0
        for ev in events:
            if ev.get("body") and ev["body"] != _REDACTED:
                ev["body"] = _REDACTED
                await self._s.put("ticket_events", ev["id"], ev)
                redacted += 1
        if redacted:
            erased.append(f"event_bodies:{redacted}")

        return {
            "service": self.service,
            "subject_ref": ref,
            "erased": erased,
            "retained": retained,
            "already_absent": not tickets and not redacted,
        }
