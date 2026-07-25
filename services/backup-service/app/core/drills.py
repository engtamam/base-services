"""The record of every restore drill — the standing evidence that DR actually works.

'When did we last PROVE we could restore everything?' is the question an auditor, a customer's
security review, and the operator's own confidence all rest on. It is answered here: each drill and
its per-target outcome, newest first.
"""
from __future__ import annotations

from typing import Any

_COLL = "backup_drills"


class DrillStore:
    def __init__(self, store) -> None:
        self._s = store

    async def record(self, drill: dict[str, Any]) -> None:
        await self._s.put(_COLL, drill["id"], drill)

    async def get(self, drill_id: str) -> dict | None:
        return await self._s.get(_COLL, drill_id)

    async def recent(self, limit: int) -> list[dict]:
        """The most recent drills, newest first — the DR-works evidence an operator reviews."""
        # sweeper: the drill log is this service's own control data, global and bounded by retention,
        # so reading it whole IS the operation, not a shortcut around an index.
        rows = await self._s.scan_all(_COLL, limit)
        return sorted(rows, key=lambda r: r.get("at", ""), reverse=True)[:limit]
