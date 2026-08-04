"""The notification outbox — a durable promise to notify, drained to notification-service.

The service never sends mail. On a notify-worthy ticket event it writes a row here FIRST, and a
scheduler drains it. That ordering is the whole point: the event bus is at-most-once, so a
notification that depended on a bus message would be lost the one time a subscriber was restarting.
A row is retried until delivered (or permanently refused), exactly like a grant.

WHO to notify is a HINT (`to`: raiser | operators) that notification-service resolves against its own
subscriptions and the pseudonym — this service holds no addresses. WHAT to say is the type's
`notify.template_prefix` plus the event, so the message copy lives in notification-service's
templates, never here.
"""
from __future__ import annotations

import logging
from typing import Any

from app.core.models import new_id, now
from app.ports.notification_sender import NotificationRefused, NotificationUnavailable

_log = logging.getLogger(__name__)
_COLL = "notifications"

PENDING, DELIVERED, FAILED = "pending", "delivered", "failed"

# Who a given event is FOR. A customer's action informs the operators; an operator's action (or a
# status move) informs the customer who raised it.
_AUDIENCE = {"created": "operators", "status_changed": "raiser"}


def _audience(event: str, data: dict[str, Any]) -> str:
    if event == "replied":
        return "raiser" if (data or {}).get("by") == "operator" else "operators"
    return _AUDIENCE.get(event, "operators")


class NotifierOutbox:
    def __init__(self, store, sender, *, cfg: dict[str, Any], clock=None) -> None:
        self._s = store
        self._sender = sender          # None ⇒ record only, deliver nothing (null driver)
        self._cfg = cfg
        self._clock = clock or (lambda: 0.0)

    async def record(self, *, tenant: str, ticket_id: str, template: str, event: str,
                     subject_ref: str, to: str, data: dict[str, Any]) -> dict:
        row = {
            "id": new_id(), "tenant": tenant, "ticket_id": ticket_id, "template": template,
            "event": event, "subject_ref": subject_ref, "to": to, "data": data or {},
            "source": f"ticket:{ticket_id}:{event}:{new_id()[:8]}",
            "status": PENDING, "attempts": 0, "next_attempt_at": 0.0, "last_error": "",
            "created": now(),
        }
        await self._s.put(_COLL, row["id"], row)
        return row

    async def deliver(self, row: dict) -> bool:
        """One delivery attempt. Never raises: a failure is recorded and retried."""
        if self._sender is None:
            return False
        try:
            ok = await self._sender.send(
                tenant=row["tenant"], template=row["template"], event=row["event"],
                ticket_id=row["ticket_id"], subject_ref=row.get("subject_ref", ""),
                to=row.get("to", "operators"), data=row.get("data", {}), source=row["source"])
        except NotificationRefused as exc:
            row["status"] = FAILED
            row["last_error"] = f"refused: {type(exc).__name__}"
            await self._s.put(_COLL, row["id"], row)
            _log.error("notification_refused", extra={"notification": row["id"]})
            return False
        except (NotificationUnavailable, Exception) as exc:  # noqa: BLE001 — any transport failure retries
            row["attempts"] = int(row.get("attempts", 0)) + 1
            base = self._cfg.get("retry_base_seconds", 30)
            cap = self._cfg.get("retry_max_seconds", 3600)
            row["next_attempt_at"] = self._clock() + min(base * (2 ** row["attempts"]), cap)
            row["last_error"] = type(exc).__name__
            await self._s.put(_COLL, row["id"], row)
            return False
        if not ok:
            # The null transport records but delivers nothing. Leave the row PENDING (and do NOT
            # count an attempt — this is not a failure, there is simply nowhere to send yet), so
            # pointing NOTIFY_DRIVER at http later delivers everything that accumulated.
            return False
        row["status"] = DELIVERED
        row["last_error"] = ""
        await self._s.put(_COLL, row["id"], row)
        return True

    async def drain(self, *, limit: int = 100) -> dict[str, int]:
        now_t = self._clock()
        rows, _ = await self._s.find(_COLL, {"status": PENDING}, limit=limit, sort=("created", "asc"))
        delivered = deferred = 0
        for row in rows:
            if row.get("next_attempt_at", 0.0) and row["next_attempt_at"] > now_t:
                deferred += 1
                continue
            if await self.deliver(row):
                delivered += 1
        return {"considered": len(rows), "delivered": delivered, "deferred": deferred}


class TicketNotifier:
    """The hook TicketService calls. Turns a ticket event into a durable outbox row."""

    def __init__(self, outbox: NotifierOutbox) -> None:
        self._outbox = outbox

    async def on_ticket_event(self, ticket, ticket_type, event: str, *, data: dict | None = None) -> None:
        prefix = (ticket_type.notify or {}).get("template_prefix", "ticket")
        await self._outbox.record(
            tenant=ticket.tenant, ticket_id=ticket.id, template=f"{prefix}.{event}", event=event,
            subject_ref=ticket.raiser_ref, to=_audience(event, data or {}), data=data or {})
