"""Provider chains built from credentials an operator can change after launch.

Today the container builds the email and SMS chains once at boot, capturing each key as a string.
That is why a credential change needs a redeploy: the value is not read again for the life of the
process. This resolves them instead, per tenant, from the credential store with the configured
environment value as the fallback.

THE HOT PATH IS THE WHOLE DESIGN PROBLEM. There are fourteen provider credentials. Resolving them
on every send would mean fourteen datastore reads and fourteen scrypt-backed decrypts per
notification, which is not a cost worth paying to support a change that happens a few times a year.
So a resolved chain is cached per tenant for a bounded window.

WHAT THE TTL ACTUALLY BUYS, and it is worth being honest about it: a rotation takes effect within
`PROVIDER_REFRESH_SECONDS`, not immediately. That is the trade. It is acceptable because the
previous credential stays valid through the store's own rotation window, so the overlap covers the
staleness rather than leaving a gap — and because the alternative, a redeploy, is measured in
hours. Set the refresh below the rotation window or the two stop overlapping; the boot guard
enforces that rather than trusting it.

A TENANT WITH NO STORED CREDENTIAL GETS THE ENVIRONMENT'S. Config remains the default and runtime
overrides it, so an existing deployment behaves exactly as it does today and nothing has to be
migrated.

BUILD FAILURES DO NOT TAKE THE SEND PATH DOWN. A provider whose credential is missing or
undecryptable is dropped from the chain with a warning rather than raising: the chain exists to
survive one provider being unusable, and refusing to build it would turn a single bad key into a
total outage for every channel.
"""
from __future__ import annotations

import logging
import time

_log = logging.getLogger(__name__)

# Which stored credential feeds which Settings field. The NAME an operator types in the panel is
# the settings key, so there is one vocabulary rather than two that drift.
EMAIL_CREDENTIALS = {
    "smtp": ("SMTP_PASSWORD",),
    "resend": ("RESEND_API_KEY",),
    "sendgrid": ("SENDGRID_API_KEY",),
    "mailgun": ("MAILGUN_API_KEY",),
    "postmark": ("POSTMARK_TOKEN",),
}
SMS_CREDENTIALS = {
    "twilio": ("TWILIO_AUTH_TOKEN",),
    "vonage": ("VONAGE_API_KEY", "VONAGE_API_SECRET"),
    "unifonic": ("UNIFONIC_APP_SID",),
    "taqnyat": ("TAQNYAT_TOKEN",),
    "msegat": ("MSEGAT_API_KEY",),
    "mobily": ("MOBILY_PASSWORD",),
    "webhook": ("SMS_WEBHOOK_BEARER",),
}
WHATSAPP_CREDENTIALS = {
    "meta": ("WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID"),
    "webhook": ("WHATSAPP_WEBHOOK_BEARER",),
}
# console has no credential, and must never be dropped for lacking one.
CREDENTIALS = {**EMAIL_CREDENTIALS, **SMS_CREDENTIALS, **WHATSAPP_CREDENTIALS}


class ProviderPool:
    """Resolves and caches per-tenant provider chains."""

    def __init__(self, settings, secret_store, runtime_config, *,
                 build_email, build_sms, build_whatsapp, build_template_manager=None,
                 ttl_s: int = 30, now=time.monotonic) -> None:
        self._s = settings
        self._secrets = secret_store          # None when no KEK is configured
        self._rc = runtime_config             # None when runtime policy is not wired
        self._build_email = build_email
        self._build_sms = build_sms
        self._build_whatsapp = build_whatsapp
        # Resolves the WhatsApp TEMPLATE manager per tenant. Reading the token/WABA is a secret read,
        # so it belongs here in core, not in a route (the API layer may not read credential
        # plaintext). Returns None to mean "no per-tenant identity — use the boot-built manager".
        self._build_template_manager = build_template_manager
        self._ttl = ttl_s
        self._now = now
        self._cache: dict[str, tuple[float, object, object, object]] = {}

    async def _credential(self, tenant: str, name: str) -> str:
        """The stored credential, or the configured one. Never raises."""
        fallback = getattr(self._s, name, "") or ""
        if self._secrets is None:
            return fallback
        try:
            return await self._secrets.get(tenant=tenant, name=name, fallback=fallback)
        except Exception:  # noqa: BLE001 — an undecryptable row must not stop the other providers
            _log.warning("credential_unreadable", extra={"credential": name})
            return fallback

    async def _chain_for(self, tenant: str, configured: list[str], key: str) -> list[str]:
        """The provider order, which an operator may reorder at runtime."""
        if self._rc is None:
            return configured
        raw = await self._rc.get(tenant=tenant, key=key, default=",".join(configured))
        picked = [c.strip() for c in str(raw).split(",") if c.strip()]
        return picked or configured

    async def _overrides(self, tenant: str) -> dict[str, str]:
        """Every provider credential, resolved once for this build."""
        out = {}
        for names in CREDENTIALS.values():
            for name in names:
                out[name] = await self._credential(tenant, name)
        return out

    async def get(self, tenant: str = "") -> tuple[object, object, object]:
        """The (email, sms, whatsapp) chains for this tenant, rebuilt at most once per TTL."""
        hit = self._cache.get(tenant)
        now = self._now()
        if hit and now < hit[0]:
            return hit[1], hit[2], hit[3]

        creds = await self._overrides(tenant)
        email = self._build_email(
            await self._chain_for(tenant, self._s.email_chain, "EMAIL_CHAIN"), creds)
        sms = self._build_sms(
            await self._chain_for(tenant, self._s.sms_chain, "SMS_CHAIN"), creds)
        whatsapp = self._build_whatsapp(
            await self._chain_for(tenant, self._s.whatsapp_chain, "WHATSAPP_CHAIN"), creds)
        self._cache[tenant] = (now + self._ttl, email, sms, whatsapp)
        return email, sms, whatsapp

    async def template_manager(self, tenant: str = ""):
        """The WhatsApp template manager scoped to this tenant's WABA, or None to use the boot one.

        A template belongs to exactly one WABA, so a multi-tenant adopter has one per tenant and
        approval is per tenant too. The token and WABA id resolve from the same credential store the
        send chains use — a read that must live in core, never a route. `None` means the tenant has
        no WABA of its own, so the caller falls back to the configured, boot-built manager, and the
        single-brand deployment behaves exactly as before."""
        if self._build_template_manager is None:
            return None
        token = await self._credential(tenant, "WHATSAPP_ACCESS_TOKEN")
        waba = await self._credential(tenant, "WHATSAPP_BUSINESS_ACCOUNT_ID")
        return self._build_template_manager(token, waba)

    async def whatsapp_phone_number_id(self, tenant: str = "") -> str:
        """The tenant's WhatsApp phone-number id, resolved from the same store the send creds use
        (config fallback for the single-brand case). The messaging limit lives on the PHONE NUMBER,
        so a per-tenant limit needs the per-tenant number. Reading it here keeps the credential read
        in core — a route may not read stored credentials."""
        return await self._credential(tenant, "WHATSAPP_PHONE_NUMBER_ID")

    async def whatsapp_access_token(self, tenant: str = "") -> str:
        """The tenant's WhatsApp business access token (config fallback for the single-brand case).

        Reading credential PLAINTEXT is a core act, never a route's — the same rule the send chains
        and the template manager follow. Used by phone-number registration, which must call Meta as
        the business without the token ever transiting the route or a log."""
        return await self._credential(tenant, "WHATSAPP_ACCESS_TOKEN")

    def invalidate(self, tenant: str | None = None) -> None:
        """Drop the cache so the next send resolves fresh.

        Called when the operator surface changes a credential, so the common case — an operator
        setting a key and immediately testing it — is instant rather than up to a TTL late. The TTL
        still exists, because another replica's cache cannot be reached from here.
        """
        if tenant is None:
            self._cache.clear()
        else:
            self._cache.pop(tenant, None)
