"""Outbound webhooks — dispatch, signed delivery, retry, circuit breaker, dead-letter, replay.

Guarantees:
  • **at-least-once**: the `event_id` is stable across every retry and sent as
    `Idempotency-Key`, so consumers can dedupe. It is NEVER regenerated per attempt.
  • **never dropped**: exhausted retries land in the dead-letter queue; a circuit-open
    endpoint keeps its deliveries queued.
  • **SSRF-safe**: every attempt re-validates the URL and pins the resolved IP (DNS can
    change between retries).
  • **sign the exact bytes sent**: the body is serialized once, signed, and that same
    buffer goes on the wire.
  • **hot path**: subscriber lookup is cached; dispatch does no table scans.
"""
from __future__ import annotations

import json
from typing import Any

from app.core import backoff as _backoff
from app.core import circuit as _circuit
from app.core import ssrf as _ssrf
from app.core.models import (DEAD, DELIVERED, DELIVERING, FAILED, QUEUED, Delivery,
                             Endpoint, Event, iso, new_id, now_ts)
from app.core.stores import DeliveryStore, EndpointStore, EventStore

# 4xx are the customer's fault and will never succeed on retry — EXCEPT these two.
_RETRYABLE_4XX = frozenset({408, 429})


def classify(status: int, transport_ok: bool) -> str:
    """→ 'delivered' | 'retryable' | 'terminal'. A blind retry on 400 hammers a customer forever."""
    if not transport_ok or status == 0:
        return "retryable"                       # timeout / DNS / connection reset
    if 200 <= status < 300:
        return "delivered"
    if status in _RETRYABLE_4XX:
        return "retryable"
    if 400 <= status < 500:
        return "terminal"
    return "retryable"                           # 5xx


class WebhookService:
    def __init__(self, *, endpoints: EndpointStore, events: EventStore, deliveries: DeliveryStore,
                 transport, signer, config: dict[str, Any], event_bus=None, auditor=None) -> None:
        self._endpoints = endpoints
        self._events = events
        self._deliveries = deliveries
        self._transport = transport
        self._signer = signer
        self._cfg = config
        self._bus = event_bus
        # Optional so tests can build the service without one; delivery is recorded here, in the
        # single funnel, so a delivery driven by the sweep loop is witnessed the same as one driven
        # by the internal deliver route — there is no second place to forget.
        self._auditor = auditor

    # ── endpoints ─────────────────────────────────────────────────────────────
    async def register(self, *, tenant: str, url: str, event_types: list[str],
                       secret: str = "", description: str = "") -> Endpoint:
        self._validate_url(url)                  # reject an SSRF target at registration time
        ep = Endpoint(id=new_id("ep_"), tenant=tenant, url=url, event_types=event_types or ["*"],
                      secrets=[secret or new_id("whsec_")], description=description, created=iso())
        return await self._endpoints.save(ep)

    async def update(self, endpoint_id: str, **kw) -> Endpoint | None:
        ep = await self._endpoints.get(endpoint_id)
        if ep is None:
            return None
        if kw.get("url"):
            self._validate_url(kw["url"])
        for k, v in kw.items():
            if v is not None and hasattr(ep, k):
                setattr(ep, k, v)
        return await self._endpoints.save(ep)

    async def rotate_secret(self, endpoint_id: str,
                            overlap_seconds: float | None = None) -> tuple[Endpoint, str] | None:
        """Mint a new primary secret and keep the PREVIOUS one alive across a self-expiring overlap.

        A signing secret is embedded in the receiver's config, and the receiver updates on its own
        schedule. So during the overlap every delivery is dual-signed with both secrets (see
        `deliver`), letting a receiver that has not yet swapped its copy still verify — then the
        previous secret lapses BY TIMESTAMP, no manual step, so it cannot outlive its window.
        overlap_seconds=0 is an immediate cutover, the right response to a leaked secret; a second
        rotation inside a window drops the oldest, so only one previous is ever live.
        """
        ep = await self._endpoints.get(endpoint_id)
        if ep is None:
            return None
        overlap = self._cfg["secret_rotation_overlap_seconds"] if overlap_seconds is None \
            else float(overlap_seconds)
        current = ep.secrets[:1]
        fresh = new_id("whsec_")
        if overlap > 0 and current:
            ep.secrets = [fresh] + current          # keep exactly one previous; drops any older
            ep.prev_secret_expires = now_ts() + overlap
        else:
            ep.secrets = [fresh]                     # cutover: no previous survives
            ep.prev_secret_expires = 0.0
        await self._endpoints.save(ep)
        return ep, fresh

    @staticmethod
    def _active_previous_secret(ep: Endpoint, now: float) -> str:
        """The previous signing secret while its overlap window is open, else "" — an expired
        previous is never signed with, so it lapses on its own without a manual step."""
        if len(ep.secrets) > 1 and ep.prev_secret_expires and now < ep.prev_secret_expires:
            return ep.secrets[1]
        return ""

    async def delete(self, endpoint_id: str) -> bool:
        ep = await self._endpoints.get(endpoint_id)
        if ep is None:
            return False
        await self._endpoints.delete(ep)
        return True

    def _validate_url(self, url: str) -> _ssrf.Target:
        return _ssrf.validate(
            url, require_https=self._cfg["require_https"],
            allow_private=self._cfg["allow_private_ips"],
            allowed_hosts=self._cfg["allowed_hosts"], denied_hosts=self._cfg["denied_hosts"])

    # ── dispatch (HOT PATH) ────────────────────────────────────────────────────
    async def dispatch(self, *, tenant: str, event_type: str, payload: dict) -> dict[str, Any]:
        """Persist the event once (stable id), fan out one queued Delivery per subscriber."""
        raw = json.dumps(payload).encode()
        if len(raw) > self._cfg["max_payload_bytes"]:
            raise ValueError("PAYLOAD_TOO_LARGE")
        event = Event(id=new_id("evt_"), tenant=tenant, type=event_type, payload=payload, created=iso())
        await self._events.save(event)

        subscribers = await self._endpoints.subscribers(tenant, event_type)   # cached
        created = []
        for ep in subscribers:
            d = Delivery(id=new_id("dlv_"), tenant=tenant, event_id=event.id, endpoint_id=ep.id,
                         status=QUEUED, next_retry_at=now_ts(), created=iso())
            await self._deliveries.save(d)
            created.append(d.id)
        return {"event_id": event.id, "deliveries": created, "subscribers": len(subscribers)}

    # ── delivery ───────────────────────────────────────────────────────────────
    async def deliver(self, delivery_id: str) -> dict[str, Any]:
        d = await self._deliveries.get(delivery_id)
        if d is None:
            return {"ok": False, "reason": "NOT_FOUND"}
        if d.status in (DELIVERED, DEAD):
            return {"ok": True, "status": d.status, "reason": "already_final"}

        ep = await self._endpoints.get(d.endpoint_id)
        event = await self._events.get(d.event_id)
        if ep is None or event is None:
            return await self._finalize(d, DEAD, error="endpoint or event missing")
        if not ep.enabled:
            return {"ok": False, "status": d.status, "reason": "endpoint_disabled"}

        now = now_ts()
        state = _circuit.state_now(circuit_state=ep.circuit_state, opened_at=ep.opened_at,
                                   now=now, reset_seconds=self._cfg["circuit_reset_seconds"])
        if not _circuit.can_attempt(state):
            return {"ok": False, "status": QUEUED, "reason": "circuit_open"}   # queued, NOT dropped

        # SSRF: re-validate + re-pin on EVERY attempt — DNS may have changed since the last one.
        try:
            target = self._validate_url(ep.url)
        except _ssrf.SsrfBlocked as exc:
            return await self._finalize(d, DEAD, error=f"ssrf_blocked: {exc}")  # terminal

        d.status = DELIVERING
        d.attempt += 1
        await self._deliveries.save(d)

        body = json.dumps({"id": event.id, "type": event.type, "created": event.created,
                           "data": event.payload}, separators=(",", ":")).encode()
        ts = int(now)
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "base-services-webhooks/1.0",
            "X-Webhook-Id": event.id,               # stable across retries
            "X-Webhook-Event": event.type,
            "X-Webhook-Timestamp": str(ts),
            "X-Webhook-Attempt": str(d.attempt),
            "Idempotency-Key": event.id,            # consumers dedupe on this
        }
        # Sign with the current secret, and — only while a rotation overlap is still open — ALSO with
        # the previous one, in the same header (t=..,v1=<fresh>,v1=<prev>). A receiver that has not yet
        # swapped its copy verifies against either; the moment the window lapses we stop including the
        # previous, so an old secret cannot outlive its timestamp. `verify` accepts any of the sigs.
        signature = self._signer.sign(body=body, secret=(ep.secrets or [""])[0], timestamp=ts)
        prev = self._active_previous_secret(ep, now)
        if signature and prev:
            prev_sig = self._signer.sign(body=body, secret=prev, timestamp=ts)
            _, _, v1 = prev_sig.partition(",")       # "t=ts,v1=<hex>" -> "v1=<hex>"
            if v1:
                signature = f"{signature},{v1}"
        if signature:
            headers["X-Webhook-Signature"] = signature

        resp = self._transport.send(url=ep.url, pinned_ip=target.ip, body=body, headers=headers,
                                    timeout_seconds=self._cfg["timeout_seconds"],
                                    max_response_bytes=self._cfg["max_response_bytes"])
        outcome = classify(resp.status, resp.ok)

        d.response_code, d.latency_ms = resp.status, resp.latency_ms
        d.response_body, d.error = resp.body[:512], resp.error
        ep.last_status, ep.last_error, ep.last_delivery_at = resp.status, resp.error, now

        if outcome == "delivered":
            ep.success_count += 1
            for k, v in _circuit.on_success(consecutive_failures=ep.consecutive_failures).items():
                setattr(ep, k, v)
            await self._endpoints.save_state(ep)
            return await self._finalize(d, DELIVERED)

        ep.failure_count += 1
        for k, v in _circuit.on_failure(consecutive_failures=ep.consecutive_failures, state=state,
                                        now=now, threshold=self._cfg["circuit_threshold"]).items():
            setattr(ep, k, v)
        await self._endpoints.save_state(ep)

        if outcome == "terminal":
            return await self._finalize(d, DEAD, error=f"terminal_status_{resp.status}")

        delay = _backoff.next_delay(d.attempt, base_seconds=self._cfg["retry_base_seconds"],
                                    max_seconds=self._cfg["retry_max_seconds"],
                                    max_attempts=self._cfg["retry_max_attempts"],
                                    seed=d.id, jitter=self._cfg["retry_jitter"])
        if delay is None:
            return await self._finalize(d, DEAD, error="retries_exhausted")   # dead-letter, not dropped
        d.status = FAILED
        d.next_retry_at = now + delay
        await self._deliveries.save(d)
        return {"ok": False, "status": d.status, "attempt": d.attempt, "retry_in": round(delay, 2)}

    async def _finalize(self, d: Delivery, status: str, *, error: str = "") -> dict[str, Any]:
        d.status = status
        d.next_retry_at = None
        if error:
            d.error = error
        await self._deliveries.save(d)
        if status == DELIVERED and self._auditor is not None:
            # Non-critical: a busy fan-out must not stall because the audit log is briefly down.
            await self._auditor.record(
                "webhook.delivered", actor="system", tenant=d.tenant,
                resource=f"delivery:{d.id}",
                metadata={"endpoint": d.endpoint_id, "event": d.event_id, "attempt": str(d.attempt)})
        if status == DEAD:
            await self._emit("webhook.dead_lettered", {"delivery_id": d.id, "event_id": d.event_id,
                                                       "endpoint_id": d.endpoint_id, "error": d.error})
        return {"ok": status == DELIVERED, "status": status, "attempt": d.attempt, "error": d.error}

    # ── worker sweep / replay ──────────────────────────────────────────────────
    async def sweep(self) -> dict[str, Any]:
        due = await self._deliveries.due(now_ts(), self._cfg["list_limit"])
        results = [await self.deliver(d.id) for d in due]
        return {"due": len(due), "delivered": sum(1 for r in results if r.get("ok"))}

    async def replay(self, delivery_id: str) -> dict[str, Any]:
        """Re-queue a dead-lettered (or failed) delivery. A DLQ nobody can replay is a landfill."""
        d = await self._deliveries.get(delivery_id)
        if d is None:
            return {"ok": False, "reason": "NOT_FOUND"}
        d.status, d.attempt, d.error, d.next_retry_at = QUEUED, 0, "", now_ts()
        await self._deliveries.save(d)
        return await self.deliver(d.id)

    async def replay_event(self, event_id: str) -> dict[str, Any]:
        deliveries = await self._deliveries.by_event(event_id, self._cfg["list_limit"])
        results = [await self.replay(d.id) for d in deliveries]
        return {"replayed": len(results), "delivered": sum(1 for r in results if r.get("ok"))}

    async def health(self, endpoint_id: str) -> dict[str, Any] | None:
        ep = await self._endpoints.get(endpoint_id)
        if ep is None:
            return None
        total = ep.success_count + ep.failure_count
        state = _circuit.state_now(circuit_state=ep.circuit_state, opened_at=ep.opened_at,
                                   now=now_ts(), reset_seconds=self._cfg["circuit_reset_seconds"])
        return {"endpoint_id": ep.id, "url": ep.url, "enabled": ep.enabled,
                "circuit_state": state, "consecutive_failures": ep.consecutive_failures,
                "success_count": ep.success_count, "failure_count": ep.failure_count,
                "success_rate": (ep.success_count / total) if total else None,
                "last_status": ep.last_status, "last_error": ep.last_error,
                "last_delivery_at": ep.last_delivery_at}

    async def _emit(self, event: str, payload: dict) -> None:
        if self._bus is None:
            return
        try:
            await self._bus.publish(event, {**payload, "at": iso()})
        except Exception:
            pass
