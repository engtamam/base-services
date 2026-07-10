"""NotificationService — orchestrates a send end to end.

preference/suppression check → load template (locale fallback) → render (safe) →
send via the channel's failover chain → record delivery. In-app also stores a
feed item. Channel-agnostic; providers are injected (plug-and-play).
"""
from __future__ import annotations

import time
from typing import Any

from app.core.models import Delivery, InAppItem, new_id
from app.core.stores import DeliveryStore, InAppStore, PreferenceStore, TemplateStore

CHANNELS = ("email", "sms", "inapp")
# Terminal outcomes we must NOT repeat on a retry. A 'failed' send is deliberately
# excluded so a genuine retry after a transient error still goes through.
_DEDUPE_STATES = ("sent", "suppressed")


class NotificationService:
    def __init__(self, *, templates: TemplateStore, prefs: PreferenceStore,
                 deliveries: DeliveryStore, inapp: InAppStore,
                 email, sms, renderer, email_from: str, default_locale: str,
                 dedupe_ttl_seconds: int = 86_400) -> None:
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
        if channel == "email":
            ok, provider = self._email.send(recipient, subject, body, body, self._email_from)
        elif channel == "sms":
            ok, provider = self._sms.send(recipient, body)
        else:  # inapp
            await self._inapp.add(InAppItem(id=new_id(), user=user_id or recipient,
                                            title=subject, body=body))
            ok, provider = True, "inapp"

        # 5) record delivery
        return await self._finish(idempotency_key, await self._record(
            recipient, channel, template_key, "sent" if ok else "failed",
            provider=provider, error="" if ok else "send failed"))

    async def _finish(self, idempotency_key: str | None, result: dict) -> dict:
        """Remember a terminal-success result so a retry with the same key won't re-send."""
        if idempotency_key and result.get("status") in _DEDUPE_STATES:
            await self._deliveries.remember_idempotent(idempotency_key, result, time.time())
        return result

    async def _record(self, recipient: str, channel: str, template_key: str, status: str,
                      provider: str = "", error: str = "") -> dict:
        d = Delivery(id=new_id(), recipient=recipient, channel=channel,
                     template_key=template_key, status=status, provider=provider, error=error)
        await self._deliveries.record(d)
        return {"delivery_id": d.id, "status": status, "provider": provider}

    # ── in-app feed ──────────────────────────────────────────────────────────
    async def feed(self, user: str) -> list[dict]:
        return await self._inapp.feed(user)

    async def mark_read(self, user: str, item_id: str) -> bool:
        return await self._inapp.mark_read(user, item_id)
