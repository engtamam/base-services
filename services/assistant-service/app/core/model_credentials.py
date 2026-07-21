"""The model provider's credential, resolved at runtime instead of captured at boot.

Today the container passes `MODEL_API_KEY` into the HTTP model adapter as a string, once. That is
precisely why changing the key needs a redeploy: the value is never read again for the life of the
process, and an operator who signs up for a different LLM provider after launch cannot switch to it
without an engineer.

THE HOT PATH IS THE DESIGN PROBLEM. Every question asked of the assistant reaches the model
adapter. Resolving the credential there would put a datastore read and a scrypt-backed decrypt in
front of each one, to support a change that happens a few times a year. So a resolved value is
cached for a bounded window, per tenant.

WHAT THE TTL COSTS, honestly: a rotation lands within the refresh window rather than instantly.
That is acceptable only because the store keeps the previous credential valid through its own
rotation window, so the overlap covers the staleness — and the boot guard refuses a refresh longer
than that window, where the overlap would stop existing. The operator surface also invalidates this
replica's cache on a write, so the common case (set a key, immediately ask a question) is instant.

CONFIG REMAINS THE DEFAULT. Nothing stored means the environment value, so a deployment that never
touches the panel behaves exactly as it does today and nothing has to be migrated.

AN UNREADABLE CREDENTIAL DOES NOT TAKE ANSWERING DOWN. A row sealed under a KEK that no longer
matches falls back to the configured key with a warning rather than raising: the provider will
reject a wrong key with an error the adapter already handles, whereas raising here turns one bad
row into a 500 on every question.
"""
from __future__ import annotations

import logging
import time

_log = logging.getLogger(__name__)

# The stored credential's NAME is the settings key it replaces, so an operator types the same
# vocabulary the deployment already uses rather than learning a second one.
MODEL_API_KEY = "MODEL_API_KEY"


class ModelCredentials:
    """Resolves and caches the model credential for a tenant."""

    def __init__(self, settings, secret_store, *, name: str = MODEL_API_KEY,
                 ttl_s: int = 30, now=time.monotonic) -> None:
        self._s = settings
        self._secrets = secret_store          # None when no KEK is configured
        self._name = name
        self._ttl = ttl_s
        self._now = now
        self._cache: dict[str, tuple[float, str]] = {}

    async def get(self, tenant: str = "") -> str:
        """The credential to authenticate with, resolved at most once per TTL."""
        hit = self._cache.get(tenant)
        now = self._now()
        if hit and now < hit[0]:
            return hit[1]

        fallback = getattr(self._s, self._name, "") or ""
        value = fallback
        if self._secrets is not None:
            try:
                value = await self._secrets.get(tenant=tenant, name=self._name, fallback=fallback)
            except Exception:  # noqa: BLE001 — an unreadable row must not fail every question
                _log.warning("credential_unreadable", extra={"credential": self._name})
        self._cache[tenant] = (now + self._ttl, value)
        return value

    def invalidate(self, tenant: str | None = None) -> None:
        """Drop the cached value so the next question resolves fresh.

        The TTL still matters: another replica's cache cannot be reached from here. This only makes
        the operator's own set-then-test loop instant rather than up to a window late.
        """
        if tenant is None:
            self._cache.clear()
        else:
            self._cache.pop(tenant, None)
