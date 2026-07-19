"""Event-bus sink — publish the event and let audit-service consume it.

The trade against the HTTP sink is worth being explicit about, because it is not "async is better".

    HTTP      the caller learns the seq and hash — it knows the event is IN the chain, positioned
              and sealed. It pays a network round trip on the action's own path.
    eventbus  no round trip on the hot path, and audit being down stops nothing. But the receipt
              is an acknowledgement of PUBLICATION, not of recording: the event is somewhere in
              flight, and if the consumer never runs, the chain stays empty while every service
              reports success.

So this returns ACCEPTED with no hash and a detail that says exactly that. Do not read the absence
of a hash as a detail — it is the substantive difference between the two sinks, and a deployment
that needs "the log definitely holds it" wants the HTTP sink.

Publishing failures are reported, never raised. If the bus refuses the event, the caller finds out
and the failure policy decides.
"""
from __future__ import annotations

from app.ports.audit_sink import ACCEPTED, FAILED, AuditEvent, AuditReceipt


class EventBusAuditSink:
    driver = "eventbus"

    def __init__(self, event_bus, topic: str) -> None:
        self._bus = event_bus
        self._topic = topic

    async def emit(self, event: AuditEvent) -> AuditReceipt:
        try:
            await self._bus.publish(self._topic, event.to_payload())
        except Exception as exc:
            return AuditReceipt(status=FAILED, detail=f"audit publish to {self._topic!r} failed: {exc}")
        return AuditReceipt(
            status=ACCEPTED,
            detail=(f"published to {self._topic!r} — accepted by the BUS, not yet sealed into the "
                    f"chain; there is no seq or hash until audit-service consumes it"),
        )
