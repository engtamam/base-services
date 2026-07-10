"""AuditRepository over the generic DataStore port. Append-only + per-partition head."""
from __future__ import annotations

from typing import Any

from app.ports.datastore import DataStore

_EVENTS = "audit_events"   # id -> event dict (with internal _partition)
_META = "audit_meta"       # partition -> {seq, head_hash}
_SCAN = 1_000_000          # upper bound for a full scan (memory/postgres)


class AuditRepository:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def head(self, partition: str) -> tuple[int, str]:
        m = await self._store.get(_META, partition)
        return (int(m["seq"]), m["head_hash"]) if m else (0, "")

    async def append(self, event: dict[str, Any], partition: str) -> None:
        stored = {**event, "_partition": partition}
        await self._store.put(_EVENTS, event["id"], stored)
        await self._store.put(_META, partition, {"seq": event["seq"], "head_hash": event["hash"]})

    async def partitions(self) -> list[str]:
        rows = await self._store.list(_EVENTS, _SCAN)
        return sorted({r.get("_partition", "global") for r in rows})

    async def iterate(self, partition: str) -> list[dict[str, Any]]:
        rows = await self._store.list(_EVENTS, _SCAN)
        rows = [r for r in rows if r.get("_partition") == partition]
        return sorted(rows, key=lambda r: r["seq"])

    async def get(self, event_id: str) -> dict[str, Any] | None:
        return await self._store.get(_EVENTS, event_id)

    async def replace(self, event: dict[str, Any]) -> None:
        """Overwrite a stored event IN PLACE — used only by retention purge /
        tests. Not exposed via the API (append-only contract)."""
        await self._store.put(_EVENTS, event["id"], event)

    async def delete(self, event_id: str) -> None:
        await self._store.delete(_EVENTS, event_id)

    async def set_head(self, partition: str, seq: int, head_hash: str) -> None:
        await self._store.put(_META, partition, {"seq": seq, "head_hash": head_hash})

    async def query(self, filters: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        rows = await self._store.list(_EVENTS, _SCAN)
        out = []
        for r in rows:
            if self._matches(r, filters):
                out.append({k: v for k, v in r.items() if not k.startswith("_")})
        out.sort(key=lambda r: r["seq"], reverse=True)  # newest first
        return out[:limit]

    @staticmethod
    def _matches(row: dict[str, Any], f: dict[str, Any]) -> bool:
        for key in ("service", "actor", "action", "resource", "tenant"):
            if f.get(key) and row.get(key) != f[key]:
                return False
        if f.get("since") and row.get("ts", "") < f["since"]:
            return False
        if f.get("until") and row.get("ts", "") > f["until"]:
            return False
        return True
