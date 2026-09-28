"""Inbound webhooks — verify, replay-protect, dedupe, persist, publish. Then return 200 fast.

HOT PATH: every third-party event (Stripe, GitHub, a payment gateway) lands here. We do
the minimum synchronously — verify the signature, dedupe, one write, publish — and let
subscribers do the work off the EventBus. No scans, exactly one write.

Security:
  • the signature covers a **timestamp**, and we reject anything outside the tolerance
    window → a captured payload cannot be replayed later.
  • dedupe is keyed on the **provider's event id**, never a body hash: two distinct events
    can carry byte-identical bodies (e.g. two `ping`s), and hashing would silently drop one.
  • an unverified payload is never persisted or published.
"""
from __future__ import annotations

from typing import Any

from app.core.models import InboundEvent, iso, new_id
from app.core.stores import InboundStore


class InboundRejected(Exception):
    """Signature invalid, stale, or malformed. The caller returns 400 — never 5xx."""


class InboundService:
    def __init__(self, *, store: InboundStore, signer, secrets: dict[str, list[str]],
                 tolerance_seconds: int = 300, max_payload_bytes: int = 1_048_576,
                 dedupe_ttl_seconds: int = 86_400, event_bus=None) -> None:
        self._store = store
        self._signer = signer
        self._secrets = secrets            # source → [secret, previous] (rotation)
        self._tolerance = tolerance_seconds
        self._max = max_payload_bytes
        self._dedupe_ttl = dedupe_ttl_seconds
        self._bus = event_bus

    async def ingest(self, *, source: str, body: bytes, signature: str,
                     provider_event_id: str, payload: dict, now: int) -> dict[str, Any]:
        if len(body) > self._max:
            raise InboundRejected("PAYLOAD_TOO_LARGE")

        secrets = self._secrets.get(source) or []
        if not secrets:
            raise InboundRejected("UNKNOWN_SOURCE")       # fail-closed: no secret → no trust

        if not self._signer.verify(body=body, header=signature, secrets=secrets,
                                   timestamp=now, tolerance_seconds=self._tolerance):
            raise InboundRejected("INVALID_SIGNATURE")    # covers replay (stale timestamp)

        if not provider_event_id:
            raise InboundRejected("MISSING_EVENT_ID")

        # With NO bus wired there is nothing to deliver, so the event is 'published' at rest — one
        # write, and the outbox sweep never considers it. With a bus, it is stored pending (False) so a
        # publish failure is recoverable (webhook-002).
        ev = InboundEvent(id=new_id("in_"), source=source, provider_event_id=provider_event_id,
                          payload=payload, received=iso(), received_ts=float(now),
                          published=(self._bus is None))
        # ATOMIC reserve-and-store: the old seen()->record() pair had a gap where two concurrent
        # duplicates both saw not-seen and both published. reserve() is a single insert-if-absent, so
        # exactly one caller wins and publishes; the other is reported as a duplicate.
        if not await self._store.reserve(ev, now=now, ttl_seconds=self._dedupe_ttl):
            return {"accepted": True, "duplicate": True, "provider_event_id": provider_event_id}
        # Publish, then flip the durable outbox flag ONLY on success (webhook-002). A bus outage leaves
        # published=False and republish_pending() re-drives it later — the event is never silently lost.
        if self._bus is not None and await self._publish(source, ev):
            await self._store.mark_published(source, provider_event_id)
        return {"accepted": True, "duplicate": False, "id": ev.id,
                "provider_event_id": provider_event_id}

    async def _publish(self, source: str, ev: InboundEvent) -> bool:
        """Best-effort publish. Returns True on success, False on a bus outage (the event stays stored
        as pending for the republish sweep). Never raises — a bus blip must not 500 the provider."""
        if self._bus is None:
            return True                   # no bus wired = nothing to deliver to; not a pending outbox
        try:
            await self._bus.publish(f"inbound.{source}", ev.to_dict())
            return True
        except Exception:
            return False

    async def republish_pending(self, limit: int = 200) -> dict[str, Any]:
        """Durable outbox sweep (webhook-002): re-publish stored inbound events whose bus publish never
        confirmed, and mark them published on success. Driven by a CronJob; without it a 'pending' event
        would just be a lost one with extra steps."""
        republished = 0
        for row in await self._store.pending_publish(limit=limit):
            ev = InboundEvent.from_dict(row) if hasattr(InboundEvent, "from_dict") else InboundEvent(
                id=row.get("id", ""), source=row.get("source", ""),
                provider_event_id=row.get("provider_event_id", ""),
                payload=row.get("payload", {}) or {}, received=row.get("received", ""),
                received_ts=float(row.get("received_ts", 0) or 0))
            if await self._publish(ev.source, ev):
                await self._store.mark_published(ev.source, ev.provider_event_id)
                republished += 1
        return {"republished": republished}
