"""Stores over DataStore — keyset-paged, tenant-scoped, no request-path scans.

Concurrency for quota is counted with a BOUNDED find (status ∈ live-states), never a full scan: we
only need to know "are we at the cap", so we read at most cap+1 rows.
"""
from __future__ import annotations

from typing import Any

from app.core.models import (
    Export, Recording, Session, Source, _LIVE_STATES,
)

_SOURCES = "sources"
_SESSIONS = "sessions"
_RECORDINGS = "recordings"
_EXPORTS = "exports"
_TRIGGERS = "triggers"

_LIVE = list(_LIVE_STATES)


class SourceStore:
    def __init__(self, store) -> None:
        self._s = store

    async def create(self, src: Source) -> Source:
        await self._s.put(_SOURCES, src.id, src.to_dict())
        return src

    async def get(self, source_id: str) -> Source | None:
        row = await self._s.get(_SOURCES, source_id)
        return Source.from_dict(row) if row else None

    async def save(self, src: Source) -> Source:
        await self._s.put(_SOURCES, src.id, src.to_dict())
        return src

    async def delete(self, source_id: str) -> None:
        await self._s.delete(_SOURCES, source_id)

    async def list_for_tenant(self, tenant: str, limit: int = 50,
                              after: str | None = None) -> tuple[list[Source], str | None]:
        rows, cursor = await self._s.find(_SOURCES, {"tenant": tenant}, limit=limit, after=after,
                                          sort=("created", "desc"))
        return [Source.from_dict(r) for r in rows], cursor


class SessionStore:
    def __init__(self, store) -> None:
        self._s = store

    async def create(self, sess: Session) -> Session:
        await self._s.put(_SESSIONS, sess.id, sess.to_dict())
        return sess

    async def get(self, session_id: str) -> Session | None:
        row = await self._s.get(_SESSIONS, session_id)
        return Session.from_dict(row) if row else None

    async def save(self, sess: Session) -> Session:
        from app.core.models import now
        sess.updated = now()
        await self._s.put(_SESSIONS, sess.id, sess.to_dict())
        return sess

    async def delete(self, session_id: str) -> None:
        await self._s.delete(_SESSIONS, session_id)

    async def list_for_tenant(self, tenant: str, limit: int = 50, after: str | None = None,
                              status: str | None = None) -> tuple[list[Session], str | None]:
        where: dict[str, Any] = {"tenant": tenant}
        if status:
            where["status"] = status
        rows, cursor = await self._s.find(_SESSIONS, where, limit=limit, after=after,
                                          sort=("created", "desc"))
        return [Session.from_dict(r) for r in rows], cursor

    async def count_live_for_tenant(self, tenant: str, cap: int) -> int:
        """Bounded: reads at most cap+1 live sessions — enough to answer 'are we at the limit'."""
        rows, _ = await self._s.find(_SESSIONS, {"tenant": tenant, "status": _LIVE},
                                     limit=max(1, cap) + 1, sort=("created", "asc"))
        return len(rows)

    async def iter_live(self, limit: int = 200, after: str | None = None) -> tuple[list[Session], str | None]:
        """Live sessions across tenants — for reconcile/sweep (a bounded, off-request sweeper page)."""
        rows, cursor = await self._s.find(_SESSIONS, {"status": _LIVE}, limit=limit, after=after,
                                          sort=("created", "asc"))
        return [Session.from_dict(r) for r in rows], cursor


class RecordingStore:
    def __init__(self, store) -> None:
        self._s = store

    async def create(self, rec: Recording) -> Recording:
        await self._s.put(_RECORDINGS, rec.id, rec.to_dict())
        return rec

    async def get(self, recording_id: str) -> Recording | None:
        row = await self._s.get(_RECORDINGS, recording_id)
        return Recording.from_dict(row) if row else None

    async def save(self, rec: Recording) -> Recording:
        await self._s.put(_RECORDINGS, rec.id, rec.to_dict())
        return rec

    async def delete(self, recording_id: str) -> None:
        await self._s.delete(_RECORDINGS, recording_id)

    async def list_for_tenant(self, tenant: str, source_id: str | None = None, limit: int = 50,
                              after: str | None = None) -> tuple[list[Recording], str | None]:
        where: dict[str, Any] = {"tenant": tenant}
        if source_id:
            where["source_id"] = source_id
        rows, cursor = await self._s.find(_RECORDINGS, where, limit=limit, after=after,
                                          sort=("started", "desc"))
        return [Recording.from_dict(r) for r in rows], cursor

    async def iter_expired(self, cutoff_iso: str, limit: int = 200,
                           after: str | None = None) -> tuple[list[Recording], str | None]:
        """Recordings started before the cutoff — the retention sweeper's page (locked ones are
        filtered by the caller, so a legal hold is never auto-deleted)."""
        rows, cursor = await self._s.find(_RECORDINGS, {"locked": False}, limit=limit, after=after,
                                          sort=("started", "asc"))
        expired = [Recording.from_dict(r) for r in rows if r.get("started", "") < cutoff_iso]
        return expired, cursor


class ExportStore:
    def __init__(self, store) -> None:
        self._s = store

    async def create(self, exp: Export) -> Export:
        await self._s.put(_EXPORTS, exp.id, exp.to_dict())
        return exp

    async def get(self, export_id: str) -> Export | None:
        row = await self._s.get(_EXPORTS, export_id)
        return Export.from_dict(row) if row else None

    async def save(self, exp: Export) -> Export:
        await self._s.put(_EXPORTS, exp.id, exp.to_dict())
        return exp


class TriggerStore:
    """Per-source triggers, stored as one row keyed by source id."""

    def __init__(self, store) -> None:
        self._s = store

    async def get(self, source_id: str) -> list[dict[str, Any]]:
        row = await self._s.get(_TRIGGERS, source_id)
        return list(row.get("triggers", [])) if row else []

    async def put(self, source_id: str, tenant: str, triggers: list[dict[str, Any]]) -> None:
        await self._s.put(_TRIGGERS, source_id,
                          {"id": source_id, "tenant": tenant, "triggers": triggers})
