"""Erasure — notification-service: what it deletes for a data subject, and what it keeps, with the reason.

Preferences and the in-app feed are the person's data and are deleted.

Delivery records are NOT deleted: they are the proof that a message was sent, which is
what you produce when someone disputes that they were notified. But the BODY is stripped —
the proof needs the fact of the send, not its personal contents. Basis: delivery_proof.

Every ack names what was ERASED and what was RETAINED **with its legal basis**. "We kept it" is
always an answer with a reason attached, never a silence — a DSAR that cannot say why data
survived has not really answered the person.

Idempotent: a DSAR retries, so erasing an already-erased subject is a no-op that still acks.
"""
from __future__ import annotations

from typing import Any

from app.core.pii import subject_ref


_PREFS = "preferences"
_INAPP = "inapp"
_DELIVERIES = "deliveries"

RETENTION_BASIS = "delivery_proof"


class NotificationErasure:
    service = "notification-service"
    category = "messages"

    def __init__(self, *, store, pii_salt: str) -> None:
        self._store = store
        self._salt = pii_salt

    async def erase(self, tenant: str, subject: str) -> dict[str, Any]:
        ref = subject_ref(self._salt, tenant, subject)
        erased: list[str] = []

        if await self._store.get(_PREFS, subject) is not None:
            await self._store.delete(_PREFS, subject)
            erased.append("preferences")

        feed = 0
        cursor = None
        while True:
            rows, cursor = await self._store.find(_INAPP, {"user": subject},
                                                  limit=500, after=cursor, sort=("ts", "asc"))
            for r in rows:
                await self._store.delete(_INAPP, r["id"])
                feed += 1
            if not cursor:
                break
        if feed:
            erased.append(f"inapp:{feed}")

        # deliveries: keep the FACT, strip the CONTENT, replace the recipient with the pseudonym
        stripped = 0
        cursor = None
        while True:
            rows, cursor = await self._store.find(_DELIVERIES, {"tenant": tenant, "to": subject},
                                                  limit=500, after=cursor, sort=("created", "asc"))
            for r in rows:
                r["to"] = ref
                r["body"] = ""
                r["subject_line"] = ""
                await self._store.put(_DELIVERIES, r["id"], r)
                stripped += 1
            if not cursor:
                break

        return {
            "service": self.service,
            "subject_ref": ref,
            "erased": erased,
            "retained": ([{"what": f"deliveries:{stripped}", "basis": RETENTION_BASIS,
                           "why": "proof a message was sent; body stripped, recipient pseudonymised"}]
                         if stripped else []),
            "already_absent": not erased and not stripped,
        }
