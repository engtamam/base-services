"""Payment credentials an operator can change after launch.

This is the service where a wrong value moves money, so the failure modes are worth stating before
the code.

A GATEWAY BUILT AT BOOT CAPTURES ITS KEY FOR THE LIFE OF THE PROCESS. That is why changing a
payment key needs a redeploy today. It also means that when a key is finally rotated under pressure
— a leak, an expiry — the change is made by whoever can deploy, at whatever hour, rather than by
whoever owns the account.

THE WEBHOOK SECRET IS THE OPPOSITE DIRECTION AND THE DANGEROUS ONE. Every other credential here
authenticates US to the gateway; the webhook secret is how we verify the gateway to us. During a
rotation the gateway is still signing with what it held when it sent — a callback already in flight
has no version to negotiate — so both the current and the previous secret must verify. Accepting
only the current one rejects those callbacks as forged, and a rejected `payment.succeeded` leaves
an invoice unpaid after the customer's money has already moved. That is why `accepted()` exists and
why this module uses it rather than `get()` for that one credential.

THE CACHE IS BOUNDED, NOT PERMANENT. Resolving on every charge would put a datastore read and a
scrypt-backed decrypt on the payment path. A resolved set is reused for a refresh window, which is
kept below the rotation window by a boot guard so the two always overlap.
"""
from __future__ import annotations

import logging
import time

_log = logging.getLogger(__name__)

# The gateway credentials, by the settings key an operator types in the panel — one vocabulary
# rather than two that drift.
GATEWAY_CREDENTIALS = ("PAYMENT_API_KEY", "PAYMENT_EXTRA_SECRET", "PAYMENT_WEBHOOK_SECRET",
                       "ZATCA_SERVICE_TOKEN")
WEBHOOK_SECRET = "PAYMENT_WEBHOOK_SECRET"


class CredentialResolver:
    def __init__(self, settings, secret_store, *, ttl_s: int = 30, now=time.monotonic) -> None:
        self._s = settings
        self._secrets = secret_store        # None when no KEK is configured
        self._ttl = ttl_s
        self._now = now
        self._cache: dict[str, tuple[float, dict[str, str]]] = {}
        # tenant -> (credential fingerprint, built gateway)
        self._gateways: dict[str, tuple[tuple, object]] = {}

    async def _one(self, tenant: str, name: str) -> str:
        fallback = getattr(self._s, name, "") or ""
        if self._secrets is None:
            return fallback
        try:
            return await self._secrets.get(tenant=tenant, name=name, fallback=fallback)
        except Exception:  # noqa: BLE001 — a bad row must not stop every other credential
            _log.warning("credential_unreadable", extra={"credential": name})
            return fallback

    async def resolve(self, tenant: str = "") -> dict[str, str]:
        """Every gateway credential for this tenant, cached for the refresh window."""
        hit = self._cache.get(tenant)
        now = self._now()
        if hit and now < hit[0]:
            return hit[1]
        out = {name: await self._one(tenant, name) for name in GATEWAY_CREDENTIALS}
        self._cache[tenant] = (now + self._ttl, out)
        return out

    async def webhook_secrets(self, tenant: str = "") -> tuple[str, ...]:
        """Every signing secret a gateway callback may currently be signed with.

        Deliberately NOT cached: a webhook arrives rarely relative to a charge, and this is the one
        credential where being a window stale means rejecting a real payment notification as
        forged. The correctness is worth the read.
        """
        fallback = getattr(self._s, WEBHOOK_SECRET, "") or ""
        if self._secrets is None:
            return (fallback,) if fallback else ()
        try:
            live = await self._secrets.accepted(tenant=tenant, name=WEBHOOK_SECRET)
        except Exception:  # noqa: BLE001
            _log.warning("webhook_secret_unreadable")
            return (fallback,) if fallback else ()
        # The configured value stays valid alongside any stored one, so a deployment that has not
        # migrated keeps verifying exactly as it does today.
        out = [s for s in live if s]
        if fallback and fallback not in out:
            out.append(fallback)
        return tuple(out)

    async def gateway(self, tenant: str, build, boot):
        """The gateway for this tenant, built ONCE per credential set rather than per request.

        Rebuilding per request looked harmless and was not: a gateway is an object, and one rebuilt
        on every call loses anything it holds between calls. Keying the cache on the resolved
        credentials means a rotation produces a new gateway and nothing else does.
        """
        resolved = await self.resolve(tenant)
        fingerprint = tuple(sorted(resolved.items()))
        hit = self._gateways.get(tenant)
        if hit and hit[0] == fingerprint:
            return hit[1]
        built = build(resolved, boot)
        self._gateways[tenant] = (fingerprint, built)
        return built

    def invalidate(self, tenant: str | None = None) -> None:
        if tenant is None:
            self._cache.clear()
            self._gateways.clear()
        else:
            self._cache.pop(tenant, None)
            self._gateways.pop(tenant, None)


class Overlay:
    """Settings, with the resolved payment credentials laid over them.

    The gateway builders read `s.PAYMENT_API_KEY` and so on. Overlaying keeps all nine adapters
    untouched — rewriting each to take credentials another way would be nine chances to miss one,
    and a miss looks exactly like a working gateway quietly charging against a stale key.
    """

    __slots__ = ("_s", "_over")

    def __init__(self, settings, over: dict[str, str]) -> None:
        self._s = settings
        self._over = over

    def __getattr__(self, name: str):
        over = object.__getattribute__(self, "_over")
        if name in over and over[name]:
            return over[name]
        return getattr(object.__getattribute__(self, "_s"), name)
