"""NotificationSender port — where a notification is actually handed to notification-service.

WHY THIS IS AN OUTBOX AND NOT A DIRECT CALL. The event bus is at-most-once: a subscriber restarting
when `ticket.replied` fires never hears it, and nothing records that it missed one. So a
notify-worthy ticket event is written down FIRST (a durable outbox row) and delivery is a retry
against that row — the same shape billing's grant outbox uses. A notification the customer was
promised survives a dropped message.

RETRYABLE vs PERMANENT are distinguished by the sender. A 5xx / transport failure is worth retrying;
a 4xx (bad template, unknown recipient) never will be, and retrying it for ever hides the
misconfiguration behind a queue that only grows.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


class NotificationUnavailable(RuntimeError):
    """notification-service could not be reached, or refused in a way worth retrying."""


class NotificationRefused(ValueError):
    """notification-service rejected this permanently. Retrying cannot help."""


@runtime_checkable
class NotificationSender(Protocol):
    driver: str

    async def send(self, *, tenant: str, template: str, event: str, ticket_id: str,
                   subject_ref: str, to: str, data: dict[str, Any], source: str) -> bool:
        """Deliver one notification. `template` + `data` are what to say; `to` is a recipient HINT
        (raiser | operators) notification-service resolves; `source` is the idempotency key it
        de-duplicates on so a retry notifies once. True when durably accepted.

        Raises NotificationUnavailable for anything worth retrying, NotificationRefused otherwise.
        """
        ...

    async def health(self) -> bool: ...
