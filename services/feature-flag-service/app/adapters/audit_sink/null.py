"""Null sink — records nothing, and says so every single time.

This is the default, because a service must boot with zero infrastructure. It is also the one thing
in the audit path that could quietly destroy the control: a null sink that returned ACCEPTED would
mean every service reported "audited" while the chain stayed empty — which is exactly the state
this phase found the platform in, except it would now be actively lying rather than merely
disconnected.

So it returns DROPPED with the reason attached, and the production boot guard refuses to start on
it. `null` is for tests and for a first local run, never for anywhere a real action happens.

It records what it was given, so a test can prove the sink was reached rather than skipped — the
difference between "we emitted and it went nowhere by design" and "we never emitted at all".
"""
from __future__ import annotations

from app.ports.audit_sink import DROPPED, AuditEvent, AuditReceipt


class NullAuditSink:
    driver = "null"

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def emit(self, event: AuditEvent) -> AuditReceipt:
        self.events.append(event)
        return AuditReceipt(
            status=DROPPED,
            detail=("null audit sink: this event was NOT recorded anywhere (by design). "
                    "Configure AUDIT_SINK_DRIVER=http to reach audit-service."),
        )
