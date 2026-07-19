"""Null transport — dispatches NOTHING, and says so.

The honest default for tests and single-service deployments. It never reports `acked`, because the
strength of a DSAR is exactly the strength of the erasures that actually happened; a transport that
quietly returned success would turn "we deleted her data" into a lie the system tells itself. In
production the boot guard refuses this driver for the same reason.
"""
from __future__ import annotations

from app.ports.privacy_transport import FAILED, EraseOutcome


class NullPrivacyTransport:
    driver = "null"

    def __init__(self) -> None:
        self.calls: list[dict] = []          # recorded, so a test can assert intent without pretence

    async def erase(self, *, url: str, token: str, request_id: str, action: str,
                    tenant: str, subject: str, timeout: int) -> EraseOutcome:
        self.calls.append({"url": url, "request_id": request_id, "action": action,
                           "tenant": tenant, "subject": subject})
        return EraseOutcome(status=FAILED,
                            detail="null transport: nothing was dispatched (by design)")
