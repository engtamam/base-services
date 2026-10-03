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

    @property
    def store(self):
        """The underlying datastore — used by the service's atomic concurrency-quota counters, which
        live in a sibling collection to the sessions."""
        return self._s

    async def create(self, sess: Session) -> Session:
        # Create via the insert-if-absent CAS (expected_version=0), NOT put(), so the row lands
        # carrying version=1. This is not cosmetic: _stop ROTATES the LIVE->terminal transition with a
        # version-pinned cas_save(), and the postgres adapter's expected_version=0 branch is a strict
        # INSERT ... ON CONFLICT DO NOTHING. A row written by put() has no version, so the first
        # _stop's cas(sess, 0, ...) is read as insert-if-absent against an already-present row ->
        # DO NOTHING -> False: the session could never reach a terminal state and its concurrency
        # reservation leaked (the memory adapter used to treat versionless as 0 and let it through, so
        # every unit test passed). See identity RefreshStore.issue. sess.id is a fresh id, so the
        # insert always wins.
        await self._s.cas(_SESSIONS, sess.id, 0, sess.to_dict())
        sess.version = 1
        return sess

    async def get(self, session_id: str) -> Session | None:
        row = await self._s.get(_SESSIONS, session_id)
        return Session.from_dict(row) if row else None

    async def save(self, sess: Session) -> Session:
        from app.core.models import now
        sess.updated = now()
        await self._s.put(_SESSIONS, sess.id, sess.to_dict())
        return sess

    async def cas_save(self, sess: Session, expected_version: int) -> bool:
        """Version-checked save: True if this write won, False if another writer changed the session
        first. Used to make the LIVE->terminal transition atomic so exactly one concurrent _stop
        releases the concurrency reservation (stream-r2-01)."""
        from app.core.models import now
        sess.updated = now()
        if await self._s.cas(_SESSIONS, sess.id, int(expected_version), sess.to_dict()):
            sess.version = int(expected_version) + 1
            return True
        return False

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

    async def count_live_global(self, cap: int) -> int:
        """Bounded count of ALL live sessions across tenants — enough to enforce the global cap."""
        rows, _ = await self._s.find(_SESSIONS, {"status": _LIVE},
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

    async def acquire_ship_lease(self, session_id: str, now: float, ttl: float) -> bool:
        """Atomically claim the right to ship this session's segments (cas insert-if-absent). Only one
        shipper at a time — otherwise concurrent sweeps double-ship a segment and lose rec.segments
        appends (last-writer-wins on save). A stale lease (past ttl, a crashed shipper) is re-claimable."""
        k = f"ship:{session_id}"
        if await self._s.cas("stream_ship_locks", k, 0, {"session": session_id, "ts": now}):
            return True
        row = await self._s.get("stream_ship_locks", k)
        if row and (now - float(row.get("ts", 0) or 0)) > ttl:
            await self._s.put("stream_ship_locks", k, {"session": session_id, "ts": now})
            return True
        return False

    async def release_ship_lease(self, session_id: str) -> None:
        await self._s.delete("stream_ship_locks", f"ship:{session_id}")

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

    async def iter_unlocked_newest(self, limit: int = 200,
                                   after: str | None = None) -> tuple[list[Recording], str | None]:
        """Non-locked recordings NEWEST-first — the byte-budget retention pass keeps the newest bytes
        up to RETENTION_MAX_BYTES and deletes everything older. Locked (legal-hold) rows are excluded,
        so a hold is never counted against or evicted by the byte budget."""
        rows, cursor = await self._s.find(_RECORDINGS, {"locked": False}, limit=limit, after=after,
                                          sort=("started", "desc"))
        return [Recording.from_dict(r) for r in rows], cursor

    async def iter_expired(self, cutoff_iso: str, limit: int = 200,
                           after: str | None = None) -> tuple[list[Recording], str | None]:
        """Recordings started before the cutoff — the retention sweeper's page (locked ones are skipped
        in Python, so a legal hold is never auto-deleted).

        stream-011: do NOT key the query on locked==False. A legacy row written before the `locked` field
        existed has no `locked` key, so {"locked": False} never matched it and it was NEVER retention-
        deleted (kept forever). Query on `trigger` (always present, closed value set) to fetch EVERY
        recording by age, and skip locked ones in Python (from_dict defaults locked to False)."""
        rows, cursor = await self._s.find(
            _RECORDINGS, {"trigger": ["continuous", "motion", "manual"]}, limit=limit, after=after,
            sort=("started", "asc"))
        recs = [Recording.from_dict(r) for r in rows]
        # Return the expired-and-unlocked ones; locked expired rows are skipped (legal hold), not stopped on.
        expired = [r for r in recs if r.started < cutoff_iso and not r.locked]
        # Rows are oldest-first, so once a page contains an AGE-non-expired row (started >= cutoff) every
        # remaining row is also non-expired — stop paging. The stop is keyed on AGE only (not on the
        # locked filter), so a page full of locked-expired rows does not falsely end the sweep.
        if any(r.started >= cutoff_iso for r in recs):
            cursor = None
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

    async def list_for_tenant(self, tenant: str, limit: int = 50, after: str | None = None,
                              status: str | None = None,
                              source_id: str | None = None) -> tuple[list[Export], str | None]:
        """One tenant's exports, newest first.

        There was no way to list these at all: an export could be created and then fetched only by
        the id the caller happened to keep. An export that a browser tab lost was unreachable —
        still occupying storage, still holding footage, and invisible to the operator responsible
        for both.

        `status` is the filter that matters, because the question is almost always "which of my
        exports are ready?" or "which failed?".
        """
        query: dict[str, Any] = {"tenant": tenant}
        if status:
            query["status"] = status
        if source_id:
            query["source_id"] = source_id
        rows, cursor = await self._s.find(_EXPORTS, query, limit=limit, after=after,
                                          sort=("created", "desc"))
        return [Export.from_dict(r) for r in rows], cursor


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
