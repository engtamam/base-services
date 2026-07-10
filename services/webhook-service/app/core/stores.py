"""Repositories over the generic DataStore, plus the CACHED subscription index.

`dispatch()` runs for every internal event, so resolving "which endpoints subscribe to
(tenant, event_type)?" must not scan the endpoint table per event. The index is cached
under `subs:<tenant>:<event_type>` and invalidated on every endpoint write.
"""
from __future__ import annotations

import json
from typing import Any

from app.core.models import DEAD, Delivery, Endpoint, Event, InboundEvent, iso
from app.ports.datastore import DataStore

_ENDPOINTS = "endpoints"
_EVENTS = "events"
_DELIVERIES = "deliveries"
_INBOUND = "inbound"
_SCAN = 1_000_000


class EndpointStore:
    def __init__(self, store: DataStore, cache=None, cache_ttl: int = 30) -> None:
        self._store = store
        self._cache = cache
        self._ttl = cache_ttl

    # ── cache helpers (never let a cache outage break delivery) ────────────────
    async def _cache_get(self, key: str) -> Any | None:
        if self._cache is None:
            return None
        try:
            raw = await self._cache.get(key)
            return json.loads(raw) if raw else None
        except Exception:
            return None

    async def _cache_set(self, key: str, value: Any) -> None:
        if self._cache is None:
            return
        try:
            await self._cache.set(key, json.dumps(value), self._ttl)
        except Exception:
            pass

    async def _invalidate(self, tenant: str) -> None:
        """Endpoint writes are rare; blow away this tenant's whole index generation."""
        if self._cache is None:
            return
        try:
            gen = await self._cache.get(f"subsgen:{tenant}")
            await self._cache.set(f"subsgen:{tenant}", str(int(gen or 0) + 1), None)
        except Exception:
            pass

    async def _generation(self, tenant: str) -> str:
        if self._cache is None:
            return "0"
        try:
            return (await self._cache.get(f"subsgen:{tenant}")) or "0"
        except Exception:
            return "0"

    # ── writes ────────────────────────────────────────────────────────────────
    async def save(self, ep: Endpoint) -> Endpoint:
        await self._store.put(_ENDPOINTS, ep.id, ep.to_dict())
        await self._invalidate(ep.tenant)
        return ep

    async def save_state(self, ep: Endpoint) -> Endpoint:
        """Circuit/stat updates: persist WITHOUT busting the subscription index
        (delivery state never changes who subscribes to what)."""
        await self._store.put(_ENDPOINTS, ep.id, ep.to_dict())
        return ep

    async def delete(self, ep: Endpoint) -> None:
        await self._store.delete(_ENDPOINTS, ep.id)
        await self._invalidate(ep.tenant)

    async def get(self, endpoint_id: str) -> Endpoint | None:
        row = await self._store.get(_ENDPOINTS, endpoint_id)
        return Endpoint.from_dict(row) if row else None

    async def list(self, tenant: str, limit: int = 100) -> list[dict]:
        rows = [r for r in await self._store.list(_ENDPOINTS, _SCAN) if r.get("tenant") == tenant]
        rows.sort(key=lambda r: r.get("created", ""))
        return rows[:limit]

    # ── the HOT PATH ──────────────────────────────────────────────────────────
    async def subscribers(self, tenant: str, event_type: str) -> list[Endpoint]:
        gen = await self._generation(tenant)
        key = f"subs:{gen}:{tenant}:{event_type}"
        cached = await self._cache_get(key)
        if cached is not None:
            return [Endpoint.from_dict(r) for r in cached]
        rows = [r for r in await self._store.list(_ENDPOINTS, _SCAN)
                if r.get("tenant") == tenant and r.get("enabled")]
        matched = [r for r in rows if Endpoint.from_dict(r).subscribes_to(event_type)]
        await self._cache_set(key, matched)
        return [Endpoint.from_dict(r) for r in matched]


class EventStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def save(self, ev: Event) -> Event:
        await self._store.put(_EVENTS, ev.id, ev.to_dict())
        return ev

    async def get(self, event_id: str) -> Event | None:
        row = await self._store.get(_EVENTS, event_id)
        return Event.from_dict(row) if row else None


class DeliveryStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def save(self, d: Delivery) -> Delivery:
        d.updated = iso()
        await self._store.put(_DELIVERIES, d.id, d.to_dict())
        return d

    async def get(self, delivery_id: str) -> Delivery | None:
        row = await self._store.get(_DELIVERIES, delivery_id)
        return Delivery.from_dict(row) if row else None

    async def list(self, tenant: str, *, status: str | None = None, limit: int = 100) -> list[dict]:
        rows = [r for r in await self._store.list(_DELIVERIES, _SCAN) if r.get("tenant") == tenant]
        if status:
            rows = [r for r in rows if r.get("status") == status]
        rows.sort(key=lambda r: r.get("created", ""), reverse=True)
        return rows[:limit]

    async def dead_letters(self, tenant: str, limit: int = 100) -> list[dict]:
        return await self.list(tenant, status=DEAD, limit=limit)

    async def due(self, now: float, limit: int = 100) -> list[Delivery]:
        out = [Delivery.from_dict(r) for r in await self._store.list(_DELIVERIES, _SCAN)
               if r.get("status") in ("queued", "failed") and r.get("next_retry_at") is not None
               and float(r["next_retry_at"]) <= now]
        return out[:limit]

    async def by_event(self, event_id: str, limit: int = 100) -> list[Delivery]:
        return [Delivery.from_dict(r) for r in await self._store.list(_DELIVERIES, _SCAN)
                if r.get("event_id") == event_id][:limit]


class InboundStore:
    """Dedupe on the PROVIDER's event id — two distinct events can share a body."""

    def __init__(self, store: DataStore) -> None:
        self._store = store

    @staticmethod
    def _key(source: str, provider_event_id: str) -> str:
        return f"{source}::{provider_event_id}"

    async def seen(self, source: str, provider_event_id: str, *, now: float = 0.0,
                   ttl_seconds: int = 0) -> bool:
        """A dedupe record older than the window no longer suppresses (bounded memory)."""
        row = await self._store.get(_INBOUND, self._key(source, provider_event_id))
        if row is None:
            return False
        if ttl_seconds and now and (now - float(row.get("received_ts", 0) or 0)) > ttl_seconds:
            return False
        return True

    async def record(self, ev: InboundEvent) -> InboundEvent:
        await self._store.put(_INBOUND, self._key(ev.source, ev.provider_event_id), ev.to_dict())
        return ev

    async def list(self, source: str | None = None, limit: int = 100) -> list[dict]:
        rows = await self._store.list(_INBOUND, _SCAN)
        if source:
            rows = [r for r in rows if r.get("source") == source]
        rows.sort(key=lambda r: r.get("received", ""), reverse=True)
        return rows[:limit]
