"""Erasure — feature-flag-service: what it deletes for a data subject, and what it keeps, with the reason.

A flag carries `user_overrides: {subject: bool}` — a per-person map, and therefore personal
data hiding inside a config object. The subject's entry is removed from every flag.

Nothing else here is about a person: a flag definition and a rollout percentage describe
the product, not the customer.

Every ack names what was ERASED and what was RETAINED **with its legal basis**. "We kept it" is
always an answer with a reason attached, never a silence — a DSAR that cannot say why data
survived has not really answered the person.

Idempotent: a DSAR retries, so erasing an already-erased subject is a no-op that still acks.
"""
from __future__ import annotations

from typing import Any

from app.core.pii import subject_ref
from app.core.stores import FlagStore


_FLAGS = "flags"
_CATALOG_MAX = 10_000
_CACHE_ONE = "flag:"          # must match FlagService's cache keys
_CACHE_ALL = "flags:all"


class FeatureFlagErasure:
    service = "feature-flag-service"
    category = "flag_overrides"

    def __init__(self, *, store, pii_salt: str, cache=None) -> None:
        self._store = store
        self._flags = FlagStore(store)
        self._salt = pii_salt
        self._cache = cache

    async def _invalidate(self, key: str) -> None:
        # Evaluation reads a CACHED flag definition (flag:<key> / flags:all). Without invalidation an
        # erased user's override would keep influencing evaluation until the cache TTL expired.
        if self._cache is None:
            return
        for k in (f"{_CACHE_ONE}{key}", _CACHE_ALL):
            try:
                await self._cache.delete(k)
            except Exception:
                pass

    async def erase(self, tenant: str, subject: str) -> dict[str, Any]:
        ref = subject_ref(self._salt, tenant, subject)
        touched = 0
        # sweeper: the flag CATALOGUE is global and small (one row per flag, not per user), and the
        # per-person data is a map INSIDE each flag — there is no index from subject to flag.
        for row in await self._store.scan_all(_FLAGS, _CATALOG_MAX):
            overrides = row.get("user_overrides") or {}
            if subject in overrides:
                # CAS-retry removal (not a raw put — that raced a concurrent admin edit) + cache
                # invalidation so evaluation stops seeing the erased override immediately.
                if await self._flags.remove_user_override(row["key"], subject):
                    await self._invalidate(row["key"])
                    touched += 1

        return {
            "service": self.service,
            "subject_ref": ref,
            "erased": [f"user_overrides:{touched}"] if touched else [],
            "retained": [],   # flag definitions describe the product, not the person
            "already_absent": touched == 0,
        }
