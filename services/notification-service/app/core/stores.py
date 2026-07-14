"""Repositories over the generic DataStore port (memory today, postgres by config)."""
from __future__ import annotations

from app.core.models import Delivery, InAppItem, Preference, Template
from app.ports.datastore import DataStore

_TEMPLATES = "templates"
_PREFS = "preferences"
_DELIVERIES = "deliveries"
_INAPP = "inapp"
_IDEMPO = "notify_idempo"    # idempotency_key -> {result, ts}


class TemplateStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    @staticmethod
    def _key(key: str, locale: str) -> str:
        return f"{key}::{locale}"

    async def upsert(self, tmpl: Template) -> Template:
        await self._store.put(_TEMPLATES, self._key(tmpl.key, tmpl.locale), tmpl.to_dict())
        return tmpl

    async def get(self, key: str, locale: str, fallback: str = "en") -> Template | None:
        row = await self._store.get(_TEMPLATES, self._key(key, locale))
        if row is None and locale != fallback:
            row = await self._store.get(_TEMPLATES, self._key(key, fallback))
        return Template.from_dict(row) if row else None

    async def list(self, limit: int = 100) -> list[dict]:
        # sweeper: the template catalogue is global - one row per template, not per tenant.
        return await self._store.scan_all(_TEMPLATES, limit)


class PreferenceStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def get(self, user: str) -> Preference:
        row = await self._store.get(_PREFS, user)
        return Preference.from_dict(row) if row else Preference(user=user)

    async def set(self, pref: Preference) -> Preference:
        await self._store.put(_PREFS, pref.user, pref.to_dict())
        return pref


class DeliveryStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def record(self, d: Delivery) -> Delivery:
        await self._store.put(_DELIVERIES, d.id, d.to_dict())
        return d

    async def list(self, limit: int = 100) -> list[dict]:
        # sweeper: an operator's view of recent delivery attempts across every tenant. Bounded
        # by the caller's limit, and not reachable from any tenant-facing route.
        return await self._store.scan_all(_DELIVERIES, limit)

    async def due(self, status: str, limit: int) -> list[Delivery]:
        """Deliveries waiting for another attempt. Pushed down by status — never a scan.

        The caller filters on `next_attempt_ts` in Python: the DataStore `where` is equality-only
        by design (no operators means nothing to inject), so "is it time yet" cannot be pushed
        down. That is fine — the set is already narrowed to `queued`, which is a small, bounded
        set even during an outage, and it shrinks as the queue drains.
        """
        rows, _ = await self._store.find(_DELIVERIES, {"status": status}, limit=limit,
                                         sort=("ts", "asc"))
        return [Delivery.from_dict(r) for r in rows]

    async def save(self, d: Delivery) -> Delivery:
        await self._store.put(_DELIVERIES, d.id, d.to_dict())
        return d

    # ── idempotency (at-least-once callers must not double-send) ───────────────
    async def idempotent_get(self, key: str, *, now: float, ttl: int) -> dict | None:
        row = await self._store.get(_IDEMPO, key)
        if not row:
            return None
        if ttl and now and (now - float(row.get("ts", 0) or 0)) > ttl:
            return None          # window elapsed → treat as a fresh send (bounded memory)
        return row.get("result")

    async def remember_idempotent(self, key: str, result: dict, now: float) -> None:
        await self._store.put(_IDEMPO, key, {"result": result, "ts": now})


class InAppStore:
    def __init__(self, store: DataStore, page_size: int = 100, scan_limit: int = 1000) -> None:
        self._store = store
        self._page_size = page_size
        self._scan_limit = scan_limit

    async def add(self, item: InAppItem) -> InAppItem:
        await self._store.put(_INAPP, item.id, item.to_dict())
        return item

    async def feed(self, user: str, limit: int | None = None) -> list[dict]:
        limit = limit or self._page_size
        # One user's feed. `FEED_SCAN_LIMIT` is now the window this read considers - the most
        # recent N of THIS user's items - rather than the number of everybody's items it had to
        # read through to find them.
        rows, _ = await self._store.find(_INAPP, {"user": user}, limit=self._scan_limit,
                                         sort=("ts", "desc"))
        # stable two-pass: newest first, then unread first (ISO timestamps sort lexically)
        rows.sort(key=lambda r: r.get("ts", ""), reverse=True)
        rows.sort(key=lambda r: bool(r.get("read", False)))
        return rows[:limit]

    async def mark_read(self, user: str, item_id: str) -> bool:
        row = await self._store.get(_INAPP, item_id)
        if not row or row.get("user") != user:
            return False
        row["read"] = True
        await self._store.put(_INAPP, item_id, row)
        return True
