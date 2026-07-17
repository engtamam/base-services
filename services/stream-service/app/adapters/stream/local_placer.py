"""LocalSessionPlacer — the session's engine runs in THIS pod's process.

Simple and correct for a single node / StatefulSet. It owns an in-process registry of
session_id → EngineHandle, and `reconcile` compares that registry to the sessions the core still
believes are live: an engine with no owning session is an orphan and is killed; a session with no
engine here is reported dead so the core can mark it. The worker-pool placer (later) satisfies the
same interface with a remote registry — the core never knows the difference.
"""
from __future__ import annotations

from app.ports.stream_ports import StreamEngine
from app.ports.stream_types import EngineSpec, EngineStats, Placement, ReconcileResult

_WORKER = "local"


class LocalSessionPlacer:
    driver = "local"

    def __init__(self, engine: StreamEngine) -> None:
        self._engine = engine
        self._by_session: dict[str, Placement] = {}

    async def start(self, session_id: str, spec: EngineSpec) -> Placement:
        handle = await self._engine.start(spec)
        placement = Placement(placer=self.driver, worker_id=_WORKER, handle=handle)
        self._by_session[session_id] = placement
        return placement

    async def stop(self, placement: Placement) -> None:
        await self._engine.stop(placement.handle)
        self._by_session = {sid: p for sid, p in self._by_session.items()
                            if p.handle.id != placement.handle.id}

    async def stats(self, placement: Placement) -> EngineStats:
        return await self._engine.stats(placement.handle)

    async def is_alive(self, placement: Placement) -> bool:
        return await self._engine.is_alive(placement.handle)

    async def reconcile(self, known_session_ids: set[str]) -> ReconcileResult:
        killed: list[str] = []
        for sid, placement in list(self._by_session.items()):
            if sid not in known_session_ids:
                # An engine we are running for a session the core no longer knows → kill it.
                await self._engine.stop(placement.handle)
                self._by_session.pop(sid, None)
                killed.append(placement.handle.id)
        dead = [sid for sid in known_session_ids if sid not in self._by_session]
        return ReconcileResult(killed_orphans=tuple(killed), dead_sessions=tuple(dead))
