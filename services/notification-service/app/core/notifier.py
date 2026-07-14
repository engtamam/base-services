"""NotificationService — orchestrates a send end to end.

preference/suppression check → load template (locale fallback) → render (safe) →
send via the channel's failover chain → record delivery. In-app also stores a
feed item. Channel-agnostic; providers are injected (plug-and-play).
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from app.core.models import Delivery, InAppItem, new_id
from app.core.retry import (
    DEAD_LETTER,
    FAILED,
    QUEUED,
    SENT,
    SUPPRESSED,
    RetryPolicy,
)
from app.core.stores import DeliveryStore, InAppStore, PreferenceStore, TemplateStore

CHANNELS = ("email", "sms", "inapp")

# How a send is attempted. The PLATFORM decides; we enforce.
INLINE, QUEUED_MODE = "inline", "queued"
DELIVERY_MODES = (INLINE, QUEUED_MODE)

# Outcomes an idempotency key must REMEMBER, so a retry with the same key does not create a second
# delivery. The DEFAULT includes `queued`, which is the fix for the reported duplicate: a message
# goes to the queue when the provider is down, and if the caller's retry (or a sweep) calls /notify
# again with the same key before the drain runs, remembering only terminal states would start a
# SECOND queued delivery — both then drain, and she gets two. `failed` stays OUT of the default so a
# genuine retry after a permanent failure may try again. The set is CONFIG (NOTIFY_DEDUPE_STATES),
# because "which outcomes are final enough to dedupe" is a platform's call, not a constant.
_DEFAULT_DEDUPE_STATES = ("sent", "suppressed", "queued")


class NotificationService:
    def __init__(self, *, templates: TemplateStore, prefs: PreferenceStore,
                 deliveries: DeliveryStore, inapp: InAppStore,
                 email, sms, renderer, email_from: str, default_locale: str,
                 dedupe_ttl_seconds: int = 86_400,
                 dedupe_states: tuple[str, ...] = _DEFAULT_DEDUPE_STATES,
                 retry: RetryPolicy | None = None,
                 delivery_mode: str = INLINE, clock=time.time) -> None:
        self._templates = templates
        self._prefs = prefs
        self._deliveries = deliveries
        self._inapp = inapp
        self._email = email
        self._sms = sms
        self._renderer = renderer
        self._email_from = email_from
        self._default_locale = default_locale
        self._dedupe_ttl = dedupe_ttl_seconds
        self._dedupe_states = tuple(dedupe_states)
        if delivery_mode not in DELIVERY_MODES:
            raise ValueError(f"unknown NOTIFY_DELIVERY_MODE {delivery_mode!r} — "
                             f"allowed: {', '.join(DELIVERY_MODES)}")
        self._mode = delivery_mode
        self._retry = retry
        self._clock = clock

    async def notify(self, *, recipient: str, channel: str, template_key: str,
                     locale: str | None = None, variables: dict[str, Any] | None = None,
                     user_id: str | None = None, event: str | None = None,
                     idempotency_key: str | None = None) -> dict:
        # Dedupe FIRST: a retried call with the same key must not re-send (duplicate OTP
        # SMS costs money; duplicate emails erode trust). Returns the original result.
        if idempotency_key:
            prior = await self._deliveries.idempotent_get(
                idempotency_key, now=time.time(), ttl=self._dedupe_ttl)
            if prior is not None:
                return {**prior, "duplicate": True}

        if channel not in CHANNELS:
            return await self._finish(idempotency_key, await self._record(
                recipient, channel, template_key, "failed", error=f"unknown channel: {channel}"))

        # 1) preferences / opt-out
        if user_id:
            pref = await self._prefs.get(user_id)
            if not pref.allows(channel, event):
                return await self._finish(idempotency_key, await self._record(
                    recipient, channel, template_key, "suppressed"))

        # 2) template (with locale fallback)
        tmpl = await self._templates.get(template_key, locale or self._default_locale, self._default_locale)
        if tmpl is None:
            return await self._record(recipient, channel, template_key, "failed",
                                      error="template not found")

        # 3) render (injection-safe)
        try:
            subject, body = self._renderer.render(tmpl.subject, tmpl.body, variables or {})
        except Exception as exc:
            return await self._record(recipient, channel, template_key, "failed",
                                      error=f"render: {type(exc).__name__}")

        # 4) send via the channel
        ok, provider, error = await self._deliver(channel, recipient, subject, body, user_id)

        # 5) record — and if it failed, decide whether it is DEAD or merely DELAYED.
        if ok:
            return await self._finish(idempotency_key, await self._record(
                recipient, channel, template_key, SENT, provider=provider, attempts=1))

        return await self._finish(idempotency_key, await self._on_failure(
            recipient=recipient, channel=channel, template_key=template_key, provider=provider,
            error=error, attempt=1,
            payload={"recipient": recipient, "channel": channel, "template_key": template_key,
                     "locale": locale, "variables": variables or {}, "user_id": user_id,
                     "event": event}))

    async def _deliver(self, channel: str, recipient: str, subject: str, body: str,
                       user_id: str | None) -> tuple[bool, str, str]:
        """Hand the message to the provider.

        The provider clients are BLOCKING (`send()` is a plain def doing network I/O). Awaiting
        them directly on the event loop means one slow provider does not merely delay this
        notification — it stalls every other request in the pod behind it. `to_thread` keeps the
        loop free, so a provider having a bad minute costs us latency on one message rather than
        availability on all of them.
        """
        try:
            if channel == "email":
                ok, provider = await asyncio.to_thread(
                    self._email.send, recipient, subject, body, body, self._email_from)
            elif channel == "sms":
                ok, provider = await asyncio.to_thread(self._sms.send, recipient, body)
            else:  # inapp — our own store, not a third party
                await self._inapp.add(InAppItem(id=new_id(), user=user_id or recipient,
                                                title=subject, body=body))
                ok, provider = True, "inapp"
            return bool(ok), provider, "" if ok else "send failed"
        except Exception as exc:
            # The exception TYPE is the classifier's only evidence, so keep it. A provider that
            # raises TimeoutError is having a bad minute; one that raises ValueError on the
            # address will raise it again for ever.
            return False, "", f"{type(exc).__name__}: {exc}"

    async def _on_failure(self, *, recipient: str, channel: str, template_key: str,
                          provider: str, error: str, attempt: int, payload: dict) -> dict:
        """A failed send is a DECISION, not an ending.

        Before this, every failure recorded `failed` and stopped. A five-minute provider outage
        silently destroyed every message sent during it — not delayed, destroyed, with the only
        trace a row nothing would ever read again.

        Now the failure is classified. If asking again could work, the delivery is QUEUED with a
        jittered backoff and a worker will try again. If it never could (a malformed address, a
        missing template), it fails immediately — retrying that is not resilience, it is a loop
        that burns provider quota to arrive at the same answer. And if we tried and ran out of
        patience, it is DEAD_LETTER: distinct from `failed`, because "we gave up" and "it was never
        going to work" are different facts, and only one of them means an outage cost us a message.
        """
        if self._retry is None or self._mode == INLINE:
            return await self._record(recipient, channel, template_key, FAILED,
                                      provider=provider, error=error, attempts=attempt)

        state = self._retry.next_state(attempt, error)
        next_ts = 0.0
        if state == QUEUED:
            next_ts = self._clock() + self._retry.backoff_ms(attempt) / 1000.0

        return await self._record(recipient, channel, template_key, state, provider=provider,
                                  error=error, attempts=attempt, next_attempt_ts=next_ts,
                                  payload=payload if state == QUEUED else {})

    async def _finish(self, idempotency_key: str | None, result: dict) -> dict:
        """Remember a terminal-success result so a retry with the same key won't re-send."""
        if idempotency_key and result.get("status") in self._dedupe_states:
            await self._deliveries.remember_idempotent(idempotency_key, result, time.time())
        return result

    async def _record(self, recipient: str, channel: str, template_key: str, status: str,
                      provider: str = "", error: str = "", attempts: int = 0,
                      next_attempt_ts: float = 0.0, payload: dict | None = None) -> dict:
        d = Delivery(id=new_id(), recipient=recipient, channel=channel,
                     template_key=template_key, status=status, provider=provider, error=error,
                     attempts=attempts, next_attempt_ts=next_attempt_ts,
                     payload=dict(payload or {}))
        await self._deliveries.record(d)
        out = {"delivery_id": d.id, "status": status, "provider": provider}
        if status == QUEUED:
            # Tell the caller the truth: not sent yet, and WHEN we will try again. A 'queued' that
            # looks like a 'sent' is the same lie in a different colour.
            out["retry_at"] = d.next_attempt_ts
            out["attempts"] = d.attempts
        if status == DEAD_LETTER:
            out["attempts"] = d.attempts
            out["error"] = error
        return out

    # ── the worker: a retry nobody runs is not a retry ───────────────────────
    async def drain(self, limit: int = 100) -> dict:
        """Re-attempt every queued delivery whose backoff has expired.

        Driven by a tick (a CronJob or a scheduler calling /internal/notifications/drain). Without
        something running this, `queued` is just a prettier word for `lost` — and that would be the
        original bug with extra steps.

        Each delivery carries the payload it needs to be re-sent, so the worker is stateless and a
        message survives the pod that first tried to send it.
        """
        if self._retry is None or self._mode == INLINE:
            return {"drained": 0, "sent": 0, "requeued": 0, "dead": 0, "failed": 0,
                    "reason": "delivery mode is inline — nothing is ever queued"}

        now = self._clock()
        due = await self._deliveries.due(QUEUED, limit)
        out = {"drained": 0, "sent": 0, "requeued": 0, "dead": 0, "failed": 0}

        for d in due:
            if d.next_attempt_ts > now:
                continue                       # backing off; its turn has not come
            out["drained"] += 1
            attempt = d.attempts + 1
            p = d.payload

            tmpl = await self._templates.get(
                d.template_key, p.get("locale") or self._default_locale, self._default_locale)
            if tmpl is None:
                # The template vanished between attempts. That will not fix itself by waiting.
                d.status, d.error, d.attempts = FAILED, "template not found", attempt
                await self._deliveries.save(d)
                out["failed"] += 1
                continue

            try:
                subject, body = self._renderer.render(tmpl.subject, tmpl.body,
                                                      p.get("variables") or {})
            except Exception as exc:
                d.status, d.error, d.attempts = FAILED, f"render: {type(exc).__name__}", attempt
                await self._deliveries.save(d)
                out["failed"] += 1
                continue

            ok, provider, error = await self._deliver(
                d.channel, d.recipient, subject, body, p.get("user_id"))

            d.attempts = attempt
            d.provider = provider or d.provider
            if ok:
                d.status, d.error, d.next_attempt_ts, d.payload = SENT, "", 0.0, {}
                out["sent"] += 1
            else:
                d.status = self._retry.next_state(attempt, error)
                d.error = error
                if d.status == QUEUED:
                    d.next_attempt_ts = now + self._retry.backoff_ms(attempt) / 1000.0
                    out["requeued"] += 1
                else:
                    d.next_attempt_ts, d.payload = 0.0, {}
                    out["dead" if d.status == DEAD_LETTER else "failed"] += 1
            await self._deliveries.save(d)

        return out

    # ── in-app feed ──────────────────────────────────────────────────────────
    async def feed(self, user: str) -> list[dict]:
        return await self._inapp.feed(user)

    async def mark_read(self, user: str, item_id: str) -> bool:
        return await self._inapp.mark_read(user, item_id)
