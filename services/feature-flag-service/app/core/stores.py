"""Repositories over the generic DataStore. Config is append-only + versioned."""
from __future__ import annotations

from typing import Any

from app.core.models import ConfigEntry, Flag, Maintenance, Segment, iso
from app.ports.datastore import DataStore

_FLAGS = "flags"                 # shared with adapters.flags.local
_SEGMENTS = "segments"
_CONFIG = "config"               # head pointer: <key>::<env>
_CONFIG_VERSIONS = "config_versions"   # append-only: <key>::<env>::<version>
_MAINTENANCE = "maintenance"
_SCAN = 1_000_000


class FlagStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def save(self, flag: Flag) -> Flag:
        await self._store.put(_FLAGS, flag.key, flag.to_dict())
        return flag

    async def get(self, key: str) -> Flag | None:
        row = await self._store.get(_FLAGS, key)
        return Flag.from_dict(row) if row else None

    async def list(self, limit: int = 500, include_archived: bool = False) -> list[dict]:
        rows = await self._store.list(_FLAGS, _SCAN)
        if not include_archived:
            rows = [r for r in rows if not r.get("archived")]
        rows.sort(key=lambda r: r.get("key", ""))
        return rows[:limit]

    async def maybe_touch(self, key: str, when: float, *, last_evaluated: float,
                          threshold_seconds: int) -> bool:
        """Persist `last_evaluated` ONLY when it's stale enough.

        Evaluation is the highest-QPS path in the platform — writing a row on every
        evaluation would mean N writes per request. Stale-flag detection needs only
        hour-grain, so we write at most once per `threshold_seconds` per flag.
        Returns True if a write happened.
        """
        if when - float(last_evaluated or 0) < threshold_seconds:
            return False
        row = await self._store.get(_FLAGS, key)
        if row is None:
            return False
        row["last_evaluated"] = when
        await self._store.put(_FLAGS, key, row)
        return True

    async def stale(self, older_than: float, limit: int = 500) -> list[dict]:
        """Flags never evaluated, or not evaluated since `older_than` — cleanup candidates."""
        out = [r for r in await self._store.list(_FLAGS, _SCAN)
               if not r.get("archived") and float(r.get("last_evaluated", 0) or 0) < older_than]
        out.sort(key=lambda r: float(r.get("last_evaluated", 0) or 0))
        return out[:limit]


class SegmentStore:
    """Named reusable audiences — defined once, referenced by many flags."""

    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def save(self, seg: Segment) -> Segment:
        await self._store.put(_SEGMENTS, seg.key, seg.to_dict())
        return seg

    async def get(self, key: str) -> Segment | None:
        row = await self._store.get(_SEGMENTS, key)
        return Segment.from_dict(row) if row else None

    async def list(self, limit: int = 500) -> list[dict]:
        return (await self._store.list(_SEGMENTS, _SCAN))[:limit]

    async def delete(self, key: str) -> None:
        await self._store.delete(_SEGMENTS, key)


class ConfigStore:
    """Versioned, append-only. A put never overwrites — it appends a new version."""

    def __init__(self, store: DataStore) -> None:
        self._store = store

    @staticmethod
    def _head(key: str, env: str) -> str:
        return f"{key}::{env}"

    async def put(self, key: str, value: Any, *, env: str = "", actor: str = "") -> ConfigEntry:
        head = await self._store.get(_CONFIG, self._head(key, env))
        version = int(head["version"]) + 1 if head else 1
        entry = ConfigEntry(key=key, value=value, version=version, env=env, actor=actor, created=iso())
        await self._store.put(_CONFIG_VERSIONS, f"{key}::{env}::{version}", entry.to_dict())
        await self._store.put(_CONFIG, self._head(key, env), entry.to_dict())
        return entry

    async def get(self, key: str, *, env: str = "") -> ConfigEntry | None:
        """Env-scoped entry wins over global ("")."""
        if env:
            row = await self._store.get(_CONFIG, self._head(key, env))
            if row:
                return ConfigEntry.from_dict(row)
        row = await self._store.get(_CONFIG, self._head(key, ""))
        return ConfigEntry.from_dict(row) if row else None

    async def history(self, key: str, *, env: str = "", limit: int = 100) -> list[dict]:
        rows = [r for r in await self._store.list(_CONFIG_VERSIONS, _SCAN)
                if r.get("key") == key and r.get("env", "") == env]
        rows.sort(key=lambda r: int(r.get("version", 0)), reverse=True)
        return rows[:limit]

    async def rollback(self, key: str, version: int, *, env: str = "", actor: str = "") -> ConfigEntry | None:
        """Roll back by APPENDING a new version holding the old content (never rewrites history)."""
        old = await self._store.get(_CONFIG_VERSIONS, f"{key}::{env}::{int(version)}")
        if old is None:
            return None
        return await self.put(key, old["value"], env=env, actor=actor)

    async def list(self, env: str = "", limit: int = 500) -> list[dict]:
        rows = [r for r in await self._store.list(_CONFIG, _SCAN) if r.get("env", "") == env]
        rows.sort(key=lambda r: r.get("key", ""))
        return rows[:limit]


class MaintenanceStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def set(self, m: Maintenance) -> Maintenance:
        m.updated = iso()
        await self._store.put(_MAINTENANCE, m.module, m.to_dict())
        return m

    async def get(self, module: str = "global") -> Maintenance:
        row = await self._store.get(_MAINTENANCE, module)
        return Maintenance.from_dict(row) if row else Maintenance(module=module)

    async def all(self) -> list[dict]:
        return await self._store.list(_MAINTENANCE, _SCAN)
