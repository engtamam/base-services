"""Verifiers built from credentials an operator can change after launch.

The container builds one verifier per claim type at boot, capturing each provider's token as a
string. That is precisely why rotating a registry credential needs a redeploy: the token is never
read again for the life of the process. This resolves them instead, per tenant, from the credential
store, with whatever the secrets port already returns as the fallback.

THE REQUEST PATH IS THE DESIGN PROBLEM. Resolving every provider credential on each check would be
a datastore read and a scrypt-backed decrypt per verification, to support a change that happens a
few times a year. So a resolved set of verifiers is cached per tenant for a bounded window.

WHAT THE TTL BUYS, honestly stated: a rotation takes effect within `PROVIDER_REFRESH_SECONDS`, not
immediately. It is acceptable because the previous credential stays valid through the store's own
rotation window, so the overlap covers the staleness — and because the alternative, a redeploy, is
measured in hours. Set the refresh below the rotation window or the two stop overlapping; the boot
guard enforces that rather than trusting it.

A TENANT WITH NO STORED CREDENTIAL GETS THE ENVIRONMENT'S. Config remains the default and runtime
overrides it, so an existing deployment behaves exactly as it does today and nothing has to be
migrated.

A CREDENTIAL THAT CANNOT BE READ DOES NOT TAKE VERIFICATION DOWN. It falls back to the configured
value with a warning, because one unreadable row must not stop the claim types whose providers are
perfectly healthy — and because this service's own rule is that an unusable provider answers ERROR
rather than pretending, which is a verdict the caller can act on.
"""
from __future__ import annotations

import logging
import time

_log = logging.getLogger(__name__)


class VerifierPool:
    """Resolves and caches the per-tenant `claim_type -> verifier` map."""

    def __init__(self, settings, secret_store, runtime_config, *,
                 build, fallback, ttl_s: int = 30, now=time.monotonic) -> None:
        self._s = settings
        self._secrets = secret_store          # None when no KEK is configured
        self._rc = runtime_config             # None when runtime policy is not wired
        self._build = build                   # (providers_spec, tokens) -> dict
        self._fallback = fallback             # secret NAME -> configured value, from the env port
        self._ttl = ttl_s
        self._now = now
        self._cache: dict[str, tuple[float, dict]] = {}

    async def _credential(self, tenant: str, name: str) -> str:
        """The stored credential, or the configured one. Never raises."""
        configured = self._fallback(name)
        if self._secrets is None:
            return configured
        try:
            return await self._secrets.get(tenant=tenant, name=name, fallback=configured)
        except Exception:  # noqa: BLE001 — an undecryptable row must not stop the other providers
            _log.warning("credential_unreadable", extra={"credential": name})
            return configured

    async def _providers_spec(self, tenant: str) -> str:
        """Which provider answers which claim type — an operator may repoint this at runtime."""
        if self._rc is None:
            return self._s.VERIFY_PROVIDERS
        raw = await self._rc.get(tenant=tenant, key="VERIFY_PROVIDERS",
                                 default=self._s.VERIFY_PROVIDERS)
        return str(raw)

    async def _tokens(self, tenant: str) -> dict[str, str]:
        """Every provider credential this deployment names, resolved once for this build."""
        return {name: await self._credential(tenant, name)
                for name in self._s.verify_secrets.values()}

    async def get(self, tenant: str = "") -> dict:
        """The `claim_type -> verifier` map for this tenant, rebuilt at most once per TTL."""
        hit = self._cache.get(tenant)
        now = self._now()
        if hit and now < hit[0]:
            return hit[1]

        verifiers = self._build(await self._providers_spec(tenant), await self._tokens(tenant))
        self._cache[tenant] = (now + self._ttl, verifiers)
        return verifiers

    def invalidate(self, tenant: str | None = None) -> None:
        """Drop the cache so the next check resolves fresh.

        Called when the operator surface changes a credential, so the common case — an operator
        setting a key and immediately testing it — is instant rather than up to a TTL late. The TTL
        still exists, because another replica's cache cannot be reached from here.
        """
        if tenant is None:
            self._cache.clear()
        else:
            self._cache.pop(tenant, None)
