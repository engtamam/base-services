"""NotificationService — orchestrates a send end to end.

preference/suppression check → load template (locale fallback) → render (safe) →
send via the channel's failover chain → record delivery. In-app also stores a
feed item. Channel-agnostic; providers are injected (plug-and-play).
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from app.adapters.consent_gate.null import NullConsentGate
from app.core.models import Delivery, InAppItem, new_id
from app.ports.consent_gate import FAIL_CLOSED
from app.core.retry import (
    DEAD_LETTER,
    FAILED,
    QUEUED,
    SENT,
    RetryPolicy,
)
from app.core.stores import DeliveryStore, InAppStore, PreferenceStore, TemplateStore

CHANNELS = ("email", "sms", "whatsapp", "inapp")


def _var_order(item):
    """Sort key for raw-mode WhatsApp variables (notification-014): numeric keys first, in NUMERIC order
    ({{2}} before {{10}}), then any named keys lexicographically. All-numeric keys sort fully numerically."""
    k = str(item[0])
    return (0, int(k)) if k.isdigit() else (1, k)

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
                 email, sms, renderer, email_from: str, default_locale: str, whatsapp=None,
                 wa_templates=None,
                 dedupe_ttl_seconds: int = 86_400,
                 dedupe_states: tuple[str, ...] = _DEFAULT_DEDUPE_STATES,
                 retry: RetryPolicy | None = None,
                 consent_gate=None, consent_purposes: dict[str, str] | None = None,
                 consent_unknown: str = FAIL_CLOSED,
                 delivery_mode: str = INLINE, clock=time.time, auditor=None,
                 release_control=None) -> None:
        self._templates = templates
        self._prefs = prefs
        self._deliveries = deliveries
        self._inapp = inapp
        self._email = email
        self._sms = sms
        self._whatsapp = whatsapp
        self._wa_templates = wa_templates      # WhatsAppTemplateRegistry | None
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
        # A null gate answers UNKNOWN, so with the default fail-closed posture a service that is
        # configured to gate but cannot reach compliance-service stops sending those notifications
        # rather than sending them unlawfully. The boot guard prevents that combination in
        # production; this is what happens if it is ever reached anyway.
        self._consent = consent_gate or NullConsentGate()
        self._consent_purposes = dict(consent_purposes or {})
        self._consent_unknown = consent_unknown
        # Recorded in _record(), the single funnel, so an inline send and a drained retry are
        # witnessed the same way. Optional so tests build the service without one.
        self._auditor = auditor
        # After a successful WhatsApp send to a `meta`-mode number, hand thread control back to the
        # Meta Business Agent (else our send leaves the customer with a muted agent). None ⇒ feature
        # off (WHATSAPP_RELEASE_THREAD_CONTROL_ENABLED). It never raises and never affects the send.
        self._release = release_control

    async def notify(self, *, recipient: str, channel: str, template_key: str,
                     tenant: str | None = None,
                     locale: str | None = None, variables: dict[str, Any] | None = None,
                     user_id: str | None = None, event: str | None = None,
                     idempotency_key: str | None = None,
                     biz_opaque_callback_data: str | None = None,
                     whatsapp_override=None, flow_token: str | None = None,
                     flow_action_data: dict[str, Any] | None = None,
                     header_media_id: str | None = None,
                     button_payload: str | None = None,
                     url_button_param: str | None = None,
                     deep_link: str | None = None, kind: str | None = None) -> dict:
        # Dedupe FIRST: a retried call with the same key must not re-send (duplicate OTP
        # SMS costs money; duplicate emails erode trust). Returns the original result.
        dup = await self._dedupe_reserve(idempotency_key, tenant or "")
        if dup is not None:
            return dup

        if channel not in CHANNELS:
            return await self._finish(idempotency_key, tenant or "", await self._record(
                recipient, channel, template_key, "failed", tenant=tenant or "",
                error=f"unknown channel: {channel}"))

        # 1) preferences / opt-out
        if user_id:
            pref = await self._prefs.get(user_id)
            if not pref.allows(channel, event):
                return await self._finish(idempotency_key, tenant or "", await self._record(
                    recipient, channel, template_key, "suppressed", tenant=tenant or ""))

        # 1a) WHATSAPP marketing opt-out. Meta's "Stop promotions" arrives keyed by the customer's
        # phone (the user_preferences webhook), which for a WhatsApp send IS the recipient. Honor it
        # even with no internal user_id, so a business-initiated send to someone who unsubscribed is
        # suppressed — the whole point of capturing the opt-out. Service replies inside the 24h window
        # (send_interactive) are contractual, not marketing, and deliberately skip this.
        if channel == "whatsapp" and not await self._wa_recipient_allows(recipient, event, tenant or ""):
            return await self._finish(idempotency_key, tenant or "", await self._record(
                recipient, channel, template_key, "suppressed", tenant=tenant or ""))

        # 1b) CONSENT — a different question from the preference above, and the one this service
        # could not previously ask. A preference is the user's channel opt-out; consent is the
        # lawful basis for a PURPOSE. Someone who never opted out of email may still never have
        # agreed to marketing, and sending on that basis is the violation regulators act on.
        #
        # Only notifications MAPPED to a purpose are gated. Everything else — an OTP, a receipt, a
        # password reset — is sent under contract rather than consent, and gating those would
        # break the platform while protecting nobody.
        purpose = self._consent_purposes.get(event or "") or self._consent_purposes.get(template_key)
        if purpose:
            if not user_id:
                # A consent-gated purpose (e.g. marketing) with NO subject to check consent for must
                # FAIL CLOSED, never silently send. Skipping the check here was the hole: a marketing
                # send with no user_id went out with no lawful-basis check and no distinguishing trace.
                return await self._finish(idempotency_key, tenant or "", await self._record(
                    recipient, channel, template_key, "consent_no_subject", tenant=tenant or "",
                    error=f"{purpose}: a consent-gated send requires a user_id to check consent for"))
            if not tenant:
                # A gated template with NO tenant is a misconfiguration, not a refusal: consent is
                # keyed by (tenant, subject, purpose), so an empty tenant can only ever deny. Logging
                # it as consent_denied is indistinguishable from a real refusal — name it distinctly
                # so a caller that dropped the tenant sees the cause instead of a plausible-looking
                # deny.
                return await self._finish(idempotency_key, tenant or "", await self._record(
                    recipient, channel, template_key, "consent_no_tenant", tenant=tenant or "",
                    error=f"{purpose}: a consent-gated send requires a tenant"))
            verdict = await self._consent.check(
                tenant=tenant, subject=user_id, purpose=purpose)
            if not verdict.permits(self._consent_unknown):
                # A distinct status from "suppressed". A preference opt-out and a missing lawful
                # basis look identical in a delivery log otherwise, and they are answered very
                # differently when somebody asks why a person stopped receiving mail.
                return await self._finish(idempotency_key, tenant or "", await self._record(
                    recipient, channel, template_key, "consent_denied", tenant=tenant or "",
                    error=f"{purpose}: {verdict.reason or verdict.status}"))

        # 1c) WHATSAPP takes a DIFFERENT path: the "template" is a Meta-APPROVED template referenced
        # by name, not one of our internal render templates. Resolve it, refuse if it is not
        # approved, substitute the caller's variables in the template's declared ORDER, and deliver.
        # An unapproved WhatsApp template cannot be delivered by Meta, so we refuse rather than
        # pretend — a silent drop would hide a real block from the operator.
        if channel == "whatsapp":
            return await self._finish(idempotency_key, tenant or "", await self._deliver_whatsapp(
                recipient=recipient, tenant=tenant or "", template_key=template_key,
                language=locale or self._default_locale, variables=variables or {},
                biz_opaque=biz_opaque_callback_data or "", whatsapp_override=whatsapp_override,
                flow_token=flow_token or "", flow_action_data=flow_action_data,
                header_media_id=header_media_id or "", button_payload=button_payload or "",
                url_button_param=url_button_param or ""))

        # 2) template (with locale fallback)
        tmpl = await self._templates.get(template_key, locale or self._default_locale, self._default_locale)
        if tmpl is None:
            # _finish (not a bare _record) so the idempotency reservation from _dedupe_reserve is
            # RELEASED on this non-remembered failure (notification-002); otherwise a legitimate retry
            # of a missing-template/render failure is silently deduped as in_progress until the TTL.
            return await self._finish(idempotency_key, tenant or "", await self._record(
                recipient, channel, template_key, "failed",
                tenant=tenant or "", error="template not found"))

        # 3) render (injection-safe)
        try:
            subject, body = self._renderer.render(tmpl.subject, tmpl.body, variables or {})
        except Exception as exc:
            return await self._finish(idempotency_key, tenant or "", await self._record(
                recipient, channel, template_key, "failed",
                tenant=tenant or "", error=f"render: {type(exc).__name__}"))

        # 4) send via the channel (deep_link is carried only by the in-app item; other channels ignore it)
        ok, provider, error = await self._deliver(channel, recipient, subject, body, user_id,
                                                  deep_link=deep_link or "", kind=kind or "info")

        # 5) record — and if it failed, decide whether it is DEAD or merely DELAYED.
        if ok:
            return await self._finish(idempotency_key, tenant or "", await self._record(
                recipient, channel, template_key, SENT, provider=provider, attempts=1,
                tenant=tenant or ""))

        return await self._finish(idempotency_key, tenant or "", await self._on_failure(
            recipient=recipient, channel=channel, template_key=template_key, provider=provider,
            error=error, attempt=1, tenant=tenant or "",
            payload={"recipient": recipient, "channel": channel, "template_key": template_key,
                     "locale": locale, "variables": variables or {}, "user_id": user_id,
                     "event": event, "tenant": tenant or ""}))

    async def _deliver_whatsapp(self, *, recipient: str, tenant: str, template_key: str,
                                language: str, variables: dict, biz_opaque: str = "",
                                whatsapp_override=None, flow_token: str = "",
                                flow_action_data: dict | None = None,
                                header_media_id: str = "", button_payload: str = "",
                                url_button_param: str = "") -> dict:
        """Resolve the APPROVED template, substitute variables in declared order, send, record.

        `whatsapp_override` sends from a SPECIFIC number (a branch's), bypassing the tenant's default
        chain — the caller has already resolved and entitled it, and refused if unknown, so here it is
        simply the provider to send through."""
        # Every delivery this method records belongs to `tenant`, so stamp it once here rather than
        # threading tenant= through each of the branch returns below.
        async def rec(*args, **kwargs):
            return await self._record(*args, tenant=tenant, **kwargs)

        wa = whatsapp_override if whatsapp_override is not None else self._whatsapp
        if wa is None:
            return await rec(recipient, "whatsapp", template_key, "failed",
                                      error="whatsapp channel not configured")
        # No registry wired ⇒ dev/raw mode: forward the name + values as-is, in NUMERIC key order.
        # notification-014: WhatsApp positional params are numbered ({{1}}..{{10}}); a lexicographic sort
        # ordered "10" before "2", silently mis-filling the template. Sort numerically when every key is
        # integer-like, else fall back to lexicographic (named params).
        if self._wa_templates is None:
            ordered = [str(v) for _, v in sorted(variables.items(), key=_var_order)]
        else:
            tmpl = await self._wa_templates.get(tenant, template_key, language)
            if tmpl is None:
                return await rec(recipient, "whatsapp", template_key, "failed",
                                          error="whatsapp template not found")
            if tmpl.status != "approved":
                return await rec(recipient, "whatsapp", template_key, "failed",
                                          error=f"whatsapp template not approved: {tmpl.status}")
            missing = [v for v in tmpl.variables if v not in variables]
            if missing:
                return await rec(recipient, "whatsapp", template_key, "failed",
                                          error=f"missing variables: {','.join(missing)}")
            ordered = [str(variables[v]) for v in tmpl.variables]
        # A transient send failure must be QUEUED + retried like email/SMS, not recorded 'failed' and
        # destroyed (notification-001). We can only retry on the DEFAULT provider chain, so a per-branch
        # whatsapp_override send that fails stays 'failed' (a queued retry would send from the wrong
        # number). The retry payload carries everything drain() needs to re-send from scratch.
        can_retry = (whatsapp_override is None and self._retry is not None and self._mode != INLINE)
        wa_payload = {"channel": "whatsapp", "recipient": recipient, "tenant": tenant,
                      "template_key": template_key, "language": language, "variables": variables,
                      "biz_opaque": biz_opaque, "flow_token": flow_token,
                      "flow_action_data": flow_action_data, "header_media_id": header_media_id,
                      "button_payload": button_payload, "url_button_param": url_button_param}

        async def _fail(error: str, provider: str = ""):
            if can_retry:
                return await self._on_failure(
                    recipient=recipient, channel="whatsapp", template_key=template_key,
                    provider=provider, error=error, attempt=1, payload=wa_payload, tenant=tenant)
            return await rec(recipient, "whatsapp", template_key, "failed",
                             provider=provider, error=error)

        try:
            ok, provider, message_id = await asyncio.to_thread(
                wa.send, recipient, template_key, language, ordered, biz_opaque,
                flow_token, flow_action_data, header_media_id, button_payload, url_button_param)
        except Exception as exc:  # noqa: BLE001
            return await _fail(f"{type(exc).__name__}: {exc}")
        if ok:
            await self._release_thread_control(wa, recipient, tenant)
            # Capture the wamid: it is the ONLY key a later receipt can be matched back to this send by.
            return await rec(recipient, "whatsapp", template_key, SENT, provider=provider,
                                      attempts=1, provider_message_id=message_id)
        return await _fail("send failed", provider=provider)

    async def _wa_resend(self, p: dict) -> tuple[bool, str, str, str]:
        """Re-send a queued WhatsApp TEMPLATE delivery from its stored payload (notification-001).

        Returns (ok, provider, message_id, error). Only the DEFAULT provider chain is used — a
        per-branch override is never queued (see _deliver_whatsapp), so retry never sends from the
        wrong number. A vanished/unapproved template or missing variable is a PERMANENT error the retry
        classifier will not re-queue.
        """
        wa = self._whatsapp
        if wa is None:
            return False, "", "", "whatsapp channel not configured"
        tenant = p.get("tenant", "")
        template_key = p.get("template_key", "")
        language = p.get("language") or self._default_locale
        variables = p.get("variables") or {}
        if self._wa_templates is None:
            ordered = [str(v) for _, v in sorted(variables.items())]
        else:
            tmpl = await self._wa_templates.get(tenant, template_key, language)
            if tmpl is None:
                return False, "", "", "whatsapp template not found"
            if tmpl.status != "approved":
                return False, "", "", f"whatsapp template not approved: {tmpl.status}"
            missing = [v for v in tmpl.variables if v not in variables]
            if missing:
                return False, "", "", f"missing variables: {','.join(missing)}"
            ordered = [str(variables[v]) for v in tmpl.variables]
        try:
            ok, provider, mid = await asyncio.to_thread(
                wa.send, p.get("recipient", ""), template_key, language, ordered,
                p.get("biz_opaque", ""), p.get("flow_token", ""), p.get("flow_action_data"),
                p.get("header_media_id", ""), p.get("button_payload", ""),
                p.get("url_button_param", ""))
        except Exception as exc:  # noqa: BLE001
            return False, "", "", f"{type(exc).__name__}: {exc}"
        if ok:
            await self._release_thread_control(wa, p.get("recipient", ""), tenant)
            return True, provider, mid, ""
        return False, provider, "", "send failed"

    async def send_interactive(self, *, recipient: str, tenant: str, interactive: dict, kind: str,
                               biz_opaque: str = "", idempotency_key: str | None = None,
                               whatsapp_override=None) -> dict:
        """Send an interactive (button/list) WhatsApp message and record it as a Delivery.

        Interactive messages go out INSIDE the 24h customer-service window a customer's own inbound
        message opens, so no template and no consent gate apply — a reply within an open conversation
        is a service message under contract, not a business-initiated one. Everything else is the
        SAME shared plumbing every WhatsApp send uses: idempotent dedupe, per-tenant (or per-branch)
        provider resolution, the tenant-scoped Delivery record that carries the wamid so a button
        reply's webhook still matches, and the audit. This is the seam that lets a caller stop
        calling Cloud API directly — one audited, rate-limited peer for interactive sends too.
        """
        dup = await self._dedupe_reserve(idempotency_key, tenant or "")
        if dup is not None:
            return dup
        wa = whatsapp_override if whatsapp_override is not None else self._whatsapp
        tk = f"interactive:{kind}"           # the Delivery's template_key slot names the message type
        if wa is None:
            return await self._finish(idempotency_key, tenant or "", await self._record(
                recipient, "whatsapp", tk, "failed", tenant=tenant,
                error="whatsapp channel not configured"))
        try:
            ok, provider, mid = await asyncio.to_thread(
                wa.send_interactive, recipient, interactive, biz_opaque)
        except Exception as exc:  # noqa: BLE001
            return await self._finish(idempotency_key, tenant or "", await self._record(
                recipient, "whatsapp", tk, "failed", tenant=tenant,
                error=f"{type(exc).__name__}: {exc}"))
        if ok:
            await self._release_thread_control(wa, recipient, tenant)
            return await self._finish(idempotency_key, tenant or "", await self._record(
                recipient, "whatsapp", tk, SENT, tenant=tenant, provider=provider, attempts=1,
                provider_message_id=mid))
        return await self._finish(idempotency_key, tenant or "", await self._record(
            recipient, "whatsapp", tk, "failed", tenant=tenant, provider=provider,
            error="send failed"))

    async def send_carousel(self, *, recipient: str, tenant: str, template_name: str, language: str,
                            components: list[dict], user_id: str | None = None, event: str = "",
                            biz_opaque: str = "", idempotency_key: str | None = None,
                            whatsapp_override=None) -> dict:
        """Send an approved CAROUSEL template with its pre-built send-time components, as a Delivery.

        UNLIKE send_interactive, a carousel is BUSINESS-INITIATED (a template, not a 24h-window reply),
        so it is subject to the SAME consent gate as any other business-initiated template: a carousel
        mapped to a marketing purpose is not sent to a subject who has not consented. Otherwise the same
        shared plumbing — idempotent dedupe, per-tenant/branch provider, the tenant-scoped Delivery
        carrying the wamid, the audit. The components are built + validated upstream (whatsapp_carousel).
        """
        dup = await self._dedupe_reserve(idempotency_key, tenant or "")
        if dup is not None:
            return dup
        tk = template_name           # the Delivery's template_key names the Meta template, as sends do
        # WhatsApp marketing opt-out (Meta "Stop promotions"), keyed by the recipient's phone — a
        # carousel is business-initiated, so an unsubscribed customer must not receive it.
        if not await self._wa_recipient_allows(recipient, event, tenant or ""):
            return await self._finish(idempotency_key, tenant or "", await self._record(
                recipient, "whatsapp", tk, "suppressed", tenant=tenant or ""))
        # Consent — same rule as notify(): only a template MAPPED to a purpose is gated, and only with a
        # subject to key on. A carousel to a consented (or contract-basis) audience passes untouched.
        purpose = self._consent_purposes.get(event or "") or self._consent_purposes.get(template_name)
        if purpose:
            if not user_id:
                # Fail closed: a consent-gated carousel with no subject to check must not send.
                return await self._finish(idempotency_key, tenant or "", await self._record(
                    recipient, "whatsapp", tk, "consent_no_subject", tenant=tenant or "",
                    error=f"{purpose}: a consent-gated send requires a user_id to check consent for"))
            if not tenant:
                return await self._finish(idempotency_key, tenant or "", await self._record(
                    recipient, "whatsapp", tk, "consent_no_tenant", tenant=tenant or "",
                    error=f"{purpose}: a consent-gated send requires a tenant"))
            verdict = await self._consent.check(tenant=tenant, subject=user_id, purpose=purpose)
            if not verdict.permits(self._consent_unknown):
                return await self._finish(idempotency_key, tenant or "", await self._record(
                    recipient, "whatsapp", tk, "consent_denied", tenant=tenant or "",
                    error=f"{purpose}: {verdict.reason or verdict.status}"))
        wa = whatsapp_override if whatsapp_override is not None else self._whatsapp
        if wa is None:
            return await self._finish(idempotency_key, tenant or "", await self._record(
                recipient, "whatsapp", tk, "failed", tenant=tenant,
                error="whatsapp channel not configured"))
        try:
            ok, provider, mid = await asyncio.to_thread(
                wa.send_template_components, recipient, template_name, language, components, biz_opaque)
        except Exception as exc:  # noqa: BLE001
            return await self._finish(idempotency_key, tenant or "", await self._record(
                recipient, "whatsapp", tk, "failed", tenant=tenant,
                error=f"{type(exc).__name__}: {exc}"))
        if ok:
            await self._release_thread_control(wa, recipient, tenant)
            return await self._finish(idempotency_key, tenant or "", await self._record(
                recipient, "whatsapp", tk, SENT, tenant=tenant, provider=provider, attempts=1,
                provider_message_id=mid))
        return await self._finish(idempotency_key, tenant or "", await self._record(
            recipient, "whatsapp", tk, "failed", tenant=tenant, provider=provider,
            error="send failed"))

    async def _release_thread_control(self, wa, recipient: str, tenant: str) -> None:
        """Hand thread control back to the Meta agent after a successful send (meta-mode numbers only).
        Delegated to ReleaseControlService, which is durable + retrying and never raises; a no-op when
        the feature is off."""
        if self._release is not None:
            await self._release.after_send(wa=wa, recipient=recipient, tenant=tenant or "")

    async def _deliver(self, channel: str, recipient: str, subject: str, body: str,
                       user_id: str | None, deep_link: str = "",
                       kind: str = "info") -> tuple[bool, str, str]:
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
            elif channel == "inapp":   # our own store, not a third party
                await self._inapp.add(InAppItem(id=new_id(), user=user_id or recipient,
                                                title=subject, body=body, deep_link=deep_link or "",
                                                kind=kind or "info"))
                ok, provider = True, "inapp"
            else:
                # notification-015: anything else (a WhatsApp send must go through _deliver_whatsapp, and
                # drain() re-sends WA via _wa_resend) is UNROUTABLE here. The old `else: inapp` silently
                # delivered such a message as an in-app notification. Fail loudly instead of mis-routing.
                return False, "", f"unroutable channel: {channel}"
            return bool(ok), provider, "" if ok else "send failed"
        except Exception as exc:
            # The exception TYPE is the classifier's only evidence, so keep it. A provider that
            # raises TimeoutError is having a bad minute; one that raises ValueError on the
            # address will raise it again for ever.
            return False, "", f"{type(exc).__name__}: {exc}"

    async def _on_failure(self, *, recipient: str, channel: str, template_key: str,
                          provider: str, error: str, attempt: int, payload: dict,
                          tenant: str = "") -> dict:
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
                                      provider=provider, error=error, attempts=attempt,
                                      tenant=tenant)

        state = self._retry.next_state(attempt, error)
        next_ts = 0.0
        if state == QUEUED:
            next_ts = self._clock() + self._retry.backoff_ms(attempt) / 1000.0

        return await self._record(recipient, channel, template_key, state, provider=provider,
                                  error=error, attempts=attempt, next_attempt_ts=next_ts,
                                  payload=payload if state == QUEUED else {}, tenant=tenant)

    async def _wa_recipient_allows(self, recipient: str, event: str | None,
                                   tenant: str = "") -> bool:
        """Whether a WhatsApp business-initiated send to this phone is allowed by its opt-out state.

        The opt-out is scoped per (tenant, phone) — see whatsapp_optout.wa_pref_key (notification-r2-02).
        We consult, and honour a stop from, ANY of: this tenant's key, the GLOBAL key (a webhook whose
        number we could not attribute), and the LEGACY raw-phone key (opt-outs written before scoping).
        Suppression is one-directional, so consulting the broader keys is always compliance-safe."""
        from app.core.whatsapp_optout import wa_pref_key
        keys = [wa_pref_key(tenant, recipient), wa_pref_key("", recipient), recipient]
        for key in keys:
            pref = await self._prefs.get(key)
            if not pref.allows("whatsapp", event):
                return False
        return True

    async def _dedupe_reserve(self, idempotency_key: str | None, tenant: str) -> dict | None:
        """Atomic dedupe: return the prior result (duplicate) if a same-key send already completed;
        else atomically RESERVE the slot and return None so THIS caller sends. If the reservation is
        lost, a concurrent send is in flight — return an in-progress duplicate rather than sending
        again (the old read-then-write let two concurrent duplicates both dispatch)."""
        if not idempotency_key:
            return None
        now = time.time()
        prior = await self._deliveries.idempotent_get(
            idempotency_key, tenant=tenant, now=now, ttl=self._dedupe_ttl)
        if prior is not None:
            return {**prior, "duplicate": True}
        if await self._deliveries.reserve_idempotent(idempotency_key, now, tenant=tenant,
                                                     ttl=self._dedupe_ttl):
            return None                                    # we own this send
        prior = await self._deliveries.idempotent_get(
            idempotency_key, tenant=tenant, now=now, ttl=self._dedupe_ttl)
        if prior is not None:
            return {**prior, "duplicate": True}
        return {"duplicate": True, "in_progress": True, "status": "in_progress"}

    async def _finish(self, idempotency_key: str | None, tenant: str, result: dict) -> dict:
        """Remember a terminal-success result so a retry with the same key won't re-send.

        Scoped by tenant: the dedupe record lives in a per-tenant slot (see _idempo_key), so one
        tenant's key never returns another tenant's result nor suppresses another tenant's send."""
        if idempotency_key and result.get("status") in self._dedupe_states:
            await self._deliveries.remember_idempotent(
                idempotency_key, result, time.time(), tenant=tenant or "")
        elif idempotency_key:
            # NOT a dedupe (remembered) state — free the reservation so a retry can send again. This
            # keeps dedupe_states configurable (a status the operator did not list is re-sendable)
            # while the reservation still blocks a CONCURRENT duplicate for the in-flight window.
            await self._deliveries.release_idempotent(idempotency_key, tenant=tenant or "")
        return result

    async def _record(self, recipient: str, channel: str, template_key: str, status: str,
                      provider: str = "", error: str = "", attempts: int = 0,
                      next_attempt_ts: float = 0.0, payload: dict | None = None,
                      provider_message_id: str = "", tenant: str = "") -> dict:
        d = Delivery(id=new_id(), recipient=recipient, channel=channel,
                     template_key=template_key, status=status, provider=provider, error=error,
                     attempts=attempts, next_attempt_ts=next_attempt_ts,
                     payload=dict(payload or {}), provider_message_id=provider_message_id,
                     tenant=tenant)
        await self._deliveries.record(d)
        if status == SENT and self._auditor is not None:
            # Non-critical; a provider that delivered must not be un-sent because the log blinked.
            # The delivery_id links to the row that holds the recipient — kept off the audit event.
            await self._auditor.record(
                "notification.sent", actor="system", resource=f"delivery:{d.id}",
                metadata={"channel": channel, "template": template_key, "provider": provider})
        out = {"delivery_id": d.id, "status": status, "provider": provider}
        if provider_message_id:
            # Returned so a caller can keep the wamid→delivery mapping itself, even before any receipt.
            out["provider_message_id"] = provider_message_id
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

            # WhatsApp deliveries re-send through the WhatsApp path, not the email/SMS template render
            # (notification-001). Without this branch drain would look up an internal render template
            # that does not exist for a WA send and immediately mark it FAILED.
            if d.channel == "whatsapp":
                ok, provider, mid, error = await self._wa_resend(p)
                d.attempts = attempt
                d.provider = provider or d.provider
                if ok:
                    d.status, d.error, d.next_attempt_ts, d.payload = SENT, "", 0.0, {}
                    d.provider_message_id = mid or d.provider_message_id
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
                continue

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
