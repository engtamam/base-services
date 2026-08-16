"""WhatsApp Embedded Signup — exchange the browser's authorization code for the shop's business
token, and store it as that tenant's WhatsApp credential.

WHY THIS LIVES HERE AND NOT IN A ROUTE. The exchange needs the Meta APP SECRET — the credential that
identifies US as the Tech Provider — and the shop's freshly-minted access token transits this process
on its way to the secret store. That token can send in the shop's name. Every argument ProviderPool
makes for reading credentials in core rather than in a route applies with more force: the app secret
must not spread to a route, and the shop token must never appear in a request/response/log. So the
route hands core four plain identifiers and gets back success-or-reason; the secrets stay in here.

WHY IT MUST BE IN-REQUEST. Meta's exchangeable code is valid for ~30 seconds. There is no queue, no
retry, no background task — the exchange happens inside the request that receives the code, or the
code dies. That is a hard latency budget, so the two Graph calls are made back-to-back and nothing
else sits between the browser and Meta.

THE FLOW (verified against Meta's Tech-Provider onboarding docs, Graph v21):
  1. GET  {graph}/oauth/access_token?client_id&client_secret&code  → the business token (a business
     integration system-user access token; long-lived, no expiry to refresh).
  2. POST {graph}/{waba_id}/subscribed_apps  (Bearer business token)  → subscribe our app to the
     customer's WABA so its webhooks reach us. Reported but non-fatal: a failed subscribe leaves a
     usable token stored, and subscription can be retried, whereas discarding the token would force
     the whole 30-second flow to start over.
  3. Store the token as this tenant's WHATSAPP_ACCESS_TOKEN, and the phone-number id / WABA id as
     the matching per-tenant credentials ProviderPool already reads — so the very next send resolves
     them. Then invalidate the pool cache so it happens without waiting out the TTL.

Phone-number registration (POST /{phone_number_id}/register) is deliberately NOT done here: it
requires a 6-digit two-step-verification PIN the business chooses, which is not part of this request.
It is a separate, PIN-carrying step.
"""
from __future__ import annotations

import asyncio
import json as _json
import logging

from app.adapters.notify_http import bearer, get_json, ok, post_json
from app.ports.errors import ConfigError

_log = logging.getLogger(__name__)


class OnboardingError(Exception):
    """A clean, caller-safe reason the exchange failed. Never carries the token or the app secret."""


class WhatsAppOnboarding:
    def __init__(self, secret_store, *, app_id: str, app_secret: str, graph_base: str,
                 on_stored=None) -> None:
        self._secrets = secret_store
        self._app_id = app_id
        self._app_secret = app_secret
        self._base = (graph_base or "https://graph.facebook.com/v26.0").rstrip("/")
        self._on_stored = on_stored          # e.g. ProviderPool.invalidate — refresh the tenant now

    @property
    def enabled(self) -> bool:
        return bool(self._app_id and self._app_secret and self._secrets is not None)

    async def exchange(self, *, tenant: str, code: str, waba_id: str,
                       phone_number_id: str, pin: str = "", actor_ref: str = "") -> dict:
        """Exchange `code` and store the tenant's credentials. Returns {ok, subscribed, registered,
        register_reason} — never the token. Raises OnboardingError with a caller-safe reason on any
        failure of the exchange itself.

        When a `pin` is supplied, the onboarded number is REGISTERED here as a fourth step (Meta
        refuses to send until a number is registered), reported like `subscribed` — non-fatal, so a
        failed register leaves a usable token the standalone route can retry against, and never
        discards a token over the ~30s code. The PIN is sealed beside the token so re-registration
        (required after some account events) can be repeated without the human re-entering it."""
        if not self.enabled:
            raise ConfigError(
                "Embedded Signup is not configured: META_APP_ID and META_APP_SECRET identify us as "
                "the Tech Provider and must be set in the environment, and a SECRETS_KEK must be "
                "present so the exchanged token can be sealed at rest.")
        if not code or not waba_id or not phone_number_id:
            raise OnboardingError("code, waba_id and phone_number_id are all required")

        # ── 1. exchange (blocking urllib off the event loop; ~30s code TTL is the whole budget) ──
        status, body = await asyncio.to_thread(
            get_json, f"{self._base}/oauth/access_token",
            {"client_id": self._app_id, "client_secret": self._app_secret, "code": code})
        if not ok(status):
            # Meta's error body can echo the code — log a REASON, never the raw body verbatim.
            _log.warning("embedded_signup_exchange_failed", extra={"status": status})
            raise OnboardingError(_reason(body) or "the authorization code could not be exchanged "
                                  "(it may have expired — the code is valid for ~30 seconds)")
        token = _token_from(body)
        if not token:
            raise OnboardingError("the exchange succeeded but returned no access token")

        # ── 2. subscribe our app to the customer's WABA (reported, non-fatal) ──
        sub_status, _sub_body = await asyncio.to_thread(
            post_json, f"{self._base}/{waba_id}/subscribed_apps", {}, bearer(token))
        subscribed = ok(sub_status)
        if not subscribed:
            _log.warning("embedded_signup_subscribe_failed", extra={"status": sub_status})

        # ── 3. store the tenant's credentials (sealed) so the next send resolves them ──
        await self._secrets.set(tenant=tenant, name="WHATSAPP_ACCESS_TOKEN", value=token,
                                actor_ref=actor_ref)
        await self._secrets.set(tenant=tenant, name="WHATSAPP_PHONE_NUMBER_ID",
                                value=phone_number_id, actor_ref=actor_ref)
        await self._secrets.set(tenant=tenant, name="WHATSAPP_BUSINESS_ACCOUNT_ID", value=waba_id,
                                actor_ref=actor_ref)
        # ── 4. register the number so it can actually SEND (reported, non-fatal — like subscribe) ──
        # Meta refuses to send from an unregistered number (error 133010), so without this the shop is
        # "connected" and mute. Only attempted when the business supplied its 6-digit PIN; a failure is
        # reported, never fatal — the token is already sealed and the standalone route can retry.
        registered = False
        register_reason = "NO_PIN"
        if pin:
            from app.core.whatsapp_register import pin_secret_name, register_number
            res = await asyncio.to_thread(
                register_number, access_token=token, phone_number_id=phone_number_id, pin=pin,
                graph_base=self._base)
            registered, register_reason = res.registered, res.reason
            if registered:
                # Seal the PIN per number so re-registration can be repeated. Sealed only on success,
                # so a wrong PIN is never stored as if it were the real one.
                await self._secrets.set(tenant=tenant, name=pin_secret_name(phone_number_id),
                                        value=pin, actor_ref=actor_ref)

        token = ""                            # drop the plaintext from this frame as soon as it's sealed
        if self._on_stored is not None:
            self._on_stored(tenant)           # invalidate the pool cache — no TTL wait

        return {"ok": True, "subscribed": subscribed,
                "registered": registered, "register_reason": register_reason}


def _token_from(body: str) -> str:
    try:
        return str(_json.loads(body).get("access_token") or "")
    except Exception:  # noqa: BLE001 — a non-JSON body means no token; the caller raises
        return ""


def _reason(body: str) -> str:
    """A short, caller-safe reason pulled from Meta's error envelope, if present."""
    try:
        msg = _json.loads(body).get("error", {}).get("message")
        return f"Meta rejected the exchange: {msg}" if msg else ""
    except Exception:  # noqa: BLE001
        return ""
