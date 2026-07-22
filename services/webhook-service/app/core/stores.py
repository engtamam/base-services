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

# Deliveries outnumber everything else here by orders of magnitude: one row per endpoint per
# event, kept for the retry history. Nothing may read that collection whole.
_SWEEP_PAGE = 500
_INBOUND_MAX = 50_000


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

    async def list(self, tenant: str, limit: int = 100, after: str | None = None) -> list[dict]:
        rows, _ = await self._store.find(_ENDPOINTS, {"tenant": tenant}, limit=limit, after=after,
                                         sort=("created", "asc"))
        return rows

    # ── the HOT PATH ──────────────────────────────────────────────────────────
    async def subscribers(self, tenant: str, event_type: str) -> list[Endpoint]:
        gen = await self._generation(tenant)
        key = f"subs:{gen}:{tenant}:{event_type}"
        cached = await self._cache_get(key)
        if cached is not None:
            return [Endpoint.from_dict(r) for r in cached]
        # The tenant and the enabled flag are pushed into the datastore. The event-type match
        # stays here: `subscribes_to` understands wildcards (`invoice.*`), which an equality
        # filter cannot express — but it now runs over one tenant's enabled endpoints, not over
        # every endpoint of every tenant. On a cache miss this used to be a full-table scan on
        # the delivery hot path, i.e. once per event.
        rows, _ = await self._store.find(_ENDPOINTS, {"tenant": tenant, "enabled": True},
                                         limit=_SWEEP_PAGE, sort=("created", "asc"))
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

    async def list(self, tenant: str, *, type_: str | None = None, limit: int = 100,
                   after: str | None = None) -> tuple[list[dict], str | None]:
        """One tenant's events, newest first.

        Events were saved and could only be fetched by an id somebody already had — so replaying a
        delivery required knowing the event id in advance, which is exactly what an operator
        investigating a failure does not have. The list is what makes replay usable.
        """
        where: dict[str, Any] = {"tenant": tenant}
        if type_:
            where["type"] = type_
        return await self._store.find(_EVENTS, where, limit=limit, after=after,
                                      sort=("created", "desc"))


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

    async def list(self, tenant: str, *, status: str | None = None, limit: int = 100,
                   after: str | None = None) -> tuple[list[dict], str | None]:
        """One tenant's deliveries, newest first.

        Returns the cursor the datastore gives us. It used to be discarded, so past the limit the
        remaining deliveries were unreachable and nothing in the response said so — on the dead
        letter queue of all places, where the whole point is to work through a backlog.
        """
        where: dict[str, Any] = {"tenant": tenant}
        if status:
            where["status"] = status
        return await self._store.find(_DELIVERIES, where, limit=limit, after=after,
                                      sort=("created", "desc"))

    async def dead_letters(self, tenant: str, limit: int = 100,
                           after: str | None = None) -> tuple[list[dict], str | None]:
        return await self.list(tenant, status=DEAD, limit=limit, after=after)

    async def due(self, now: float, limit: int = 100) -> list[Delivery]:
        """The retry queue. `status` is pushed down; the due time is compared here.

        This is the sweeper, but it must not read DELIVERED rows — those are the overwhelming
        majority and they are never due. Filtering to the two retryable statuses first is what
        keeps the queue proportional to the backlog rather than to the history.
        """
        out: list[Delivery] = []
        cursor = None
        while len(out) < limit:
            rows, cursor = await self._store.find(_DELIVERIES, {"status": ["queued", "failed"]},
                                                  limit=_SWEEP_PAGE, after=cursor,
                                                  sort=("created", "asc"))
            out.extend(Delivery.from_dict(r) for r in rows
                       if r.get("next_retry_at") is not None and float(r["next_retry_at"]) <= now)
            if not cursor:
                break
        return out[:limit]

    async def by_event(self, event_id: str, limit: int = 100) -> list[Delivery]:
        rows, _ = await self._store.find(_DELIVERIES, {"event_id": event_id}, limit=limit,
                                         sort=("created", "asc"))
        return [Delivery.from_dict(r) for r in rows]


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

    async def list(self, source: str | None = None, limit: int = 100,
                   after: str | None = None) -> list[dict]:
        if source:
            rows, _ = await self._store.find(_INBOUND, {"source": source}, limit=limit,
                                             after=after, sort=("received", "desc"))
            return rows
        # sweeper: an operator inspecting inbound traffic across every source. Bounded.
        rows = await self._store.scan_all(_INBOUND, _INBOUND_MAX)
        rows.sort(key=lambda r: r.get("received", ""), reverse=True)
        return rows[:limit]
