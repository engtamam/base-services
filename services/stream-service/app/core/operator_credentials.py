"""Resolving one credential that an operator may have replaced since boot.

A recording sink captured its token as a string at construction, which is precisely why changing it
needed a redeploy: the value was never read again for the life of the process. This makes the read
happen at use time instead.

CONFIG IS THE FALLBACK, NOT THE OTHER WAY AROUND. A deployment with nothing stored resolves the
environment value on every call and behaves exactly as it did before — nothing has to be migrated,
and a service booted without a KEK never notices this exists.

THE CACHE IS NOT AN OPTIMISATION, IT IS THE REASON THIS IS AFFORDABLE. A sink authenticates on
every segment; without a cache a one-minute segment would cost a datastore read and an AES open per
upload, per session, forever, to support a change made a few times a year. The trade is that a
rotation lands within the refresh window rather than instantly — survivable only because the
previous credential stays valid through the store's rotation window, and the boot guard refuses a
refresh longer than that window so the two always overlap.

AN UNREADABLE STORED CREDENTIAL IS NOT FATAL HERE. If the KEK no longer matches what sealed it,
falling back to the configured value keeps recording rather than dropping footage that cannot be
re-taken; the failure is already loud in the operator surface, which is where it gets fixed.
"""
from __future__ import annotations

import time


class CredentialResolver:
    """The current value of one named credential: operator-stored if set, configured otherwise."""

    def __init__(self, store, name: str, fallback: str, *, tenant: str = "",
                 ttl_s: float = 30.0, now=time.monotonic) -> None:
        self._store = store          # a SecretStore, or None when no KEK is configured
        self._name = name
        self._fallback = fallback
        self._tenant = tenant
        self._ttl = ttl_s
        self._now = now
        self._cached: str | None = None
        self._until: float = 0.0

    async def get(self) -> str:
        if self._store is None:
            return self._fallback
        now = self._now()
        if self._cached is not None and now < self._until:
            return self._cached
        try:
            value = await self._store.get(tenant=self._tenant, name=self._name,
                                          fallback=self._fallback)
        except Exception:   # noqa: BLE001 — undecryptable, or the store is momentarily down
            value = self._fallback
        self._cached, self._until = value, now + self._ttl
        return value

    def invalidate(self) -> None:
        """Drop this replica's copy so a change made in the panel is visible on the next segment.

        Only this replica: another process cannot be reached from here, which is what the refresh
        window is still for. This makes the common case — set a token, immediately record a test
        clip — instant rather than up to a window late.
        """
        self._cached, self._until = None, 0.0
