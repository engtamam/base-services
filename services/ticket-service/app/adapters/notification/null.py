"""No notification transport wired — the default.

An adopter who does not run notification-service must not acquire a background task that fails for
ever against a service they never deployed. The outbox still RECORDS every notification, so pointing
NOTIFY_DRIVER at http later delivers everything that accumulated rather than starting from empty.
"""
from __future__ import annotations

from typing import Any


class NullNotificationSender:
    driver = "null"

    async def send(self, *, tenant: str, template: str, event: str, ticket_id: str,
                   subject_ref: str, to: str, data: dict[str, Any], source: str) -> bool:
        return False

    async def health(self) -> bool:
        return True
