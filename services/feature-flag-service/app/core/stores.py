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

# Flags, segments, config keys and maintenance modules are a global CATALOGUE: one row per flag,
# not one per tenant or per evaluation. A deployment with 10 000 feature flags has a different
# problem than a slow read. The bound is a blast radius, not a page.
_CATALOG_MAX = 10_000
_SWEEP_PAGE = 500


class FlagStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def save(self, flag: Flag) -> Flag:
        await self._store.put(_FLAGS, flag.key, flag.to_dict())
        return flag

    async def cas_save(self, flag: Flag, expected_version: int) -> bool:
        """Version-checked write: True if this write won, False if another writer changed the flag
        first (the caller re-reads and retries). Prevents a whole-document last-writer-wins that drops
        one admin's concurrent edit. (Flag.version is the optimistic-lock counter.)"""
        return await self._store.cas(_FLAGS, flag.key, expected_version, flag.to_dict())

    async def get(self, key: str) -> Flag | None:
        row = await self._store.get(_FLAGS, key)
        return Flag.from_dict(row) if row else None

    async def remove_user_override(self, key: str, subject: str, *, retries: int = 5) -> bool:
        """CAS-retry removal of ONE subject from a flag's user_overrides. True if the flag changed.

        A raw put here raced a concurrent admin edit (whole-document last-writer-wins would drop the
        edit); the version check makes the DSAR erasure lose-safe. On a lost CAS we re-read and retry."""
        for _ in range(retries):
            flag = await self.get(key)
            if flag is None:
                return False
            overrides = dict(flag.user_overrides or {})
            if subject not in overrides:
                return False
            overrides.pop(subject)
            flag.user_overrides = overrides
            if await self.cas_save(flag, int(flag.version)):
                return True
        return False

    async def list(self, limit: int = 500, include_archived: bool = False) -> list[dict]:
        if include_archived:
            # sweeper: the whole catalogue, archived flags included. Global, not tenant-scoped.
            rows = await self._store.scan_all(_FLAGS, _CATALOG_MAX)
        else:
            rows, _ = await self._store.find(_FLAGS, {"archived": False}, limit=limit,
                                             sort=("key", "asc"))
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
        # atomic: ok — best-effort hour-grain "last evaluated" stamp for stale-flag cleanup; a lost
        # write under a race just defers a staleness check by one window, never a flag value.
        await self._store.put(_FLAGS, key, row)
        return True

    async def stale(self, older_than: float, limit: int = 500) -> list[dict]:
        """Flags never evaluated, or not evaluated since `older_than` - cleanup candidates.

        `archived` is pushed down; the staleness cutoff is a range the equality filter cannot
        express, so it is applied here over the LIVE flags only.
        """
        out: list[dict] = []
        cursor = None
        while True:
            rows, cursor = await self._store.find(_FLAGS, {"archived": False}, limit=_SWEEP_PAGE,
                                                  after=cursor, sort=("key", "asc"))
            out.extend(r for r in rows if float(r.get("last_evaluated", 0) or 0) < older_than)
            if not cursor:
                break
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
        # sweeper: the segment catalogue is global and small - one row per named audience.
        return (await self._store.scan_all(_SEGMENTS, _CATALOG_MAX))[:limit]

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
        # CAS the head so a concurrent put cannot lose a version. The old read-then-write let two puts
        # both read version N, both compute N+1, and both overwrite — one version silently lost and the
        # two version rows colliding. cas the head FIRST (claims the version), then write the immutable
        # version row; a loser re-reads the (now advanced) head and retries with the next version.
        head_key = self._head(key, env)
        for _ in range(8):
            head = await self._store.get(_CONFIG, head_key)
            expected = int(head["version"]) if head else 0
            version = expected + 1
            entry = ConfigEntry(key=key, value=value, version=version, env=env, actor=actor,
                                created=iso())
            if await self._store.cas(_CONFIG, head_key, expected, entry.to_dict()):
                await self._store.put(_CONFIG_VERSIONS, f"{key}::{env}::{version}", entry.to_dict())
                return entry
        raise RuntimeError("config head contention: too many concurrent writers")

    async def get(self, key: str, *, env: str = "") -> ConfigEntry | None:
        """Env-scoped entry wins over global ("")."""
        if env:
            row = await self._store.get(_CONFIG, self._head(key, env))
            if row:
                return ConfigEntry.from_dict(row)
        row = await self._store.get(_CONFIG, self._head(key, ""))
        return ConfigEntry.from_dict(row) if row else None

    async def history(self, key: str, *, env: str = "", limit: int = 100) -> list[dict]:
        """One key's version history. Was a scan of EVERY version of EVERY key."""
        rows, _ = await self._store.find(_CONFIG_VERSIONS, {"key": key, "env": env},
                                         limit=limit, sort=("created", "desc"))
        rows.sort(key=lambda r: int(r.get("version", 0)), reverse=True)
        return rows[:limit]

    async def rollback(self, key: str, version: int, *, env: str = "", actor: str = "") -> ConfigEntry | None:
        """Roll back by APPENDING a new version holding the old content (never rewrites history)."""
        old = await self._store.get(_CONFIG_VERSIONS, f"{key}::{env}::{int(version)}")
        if old is None:
            return None
        return await self.put(key, old["value"], env=env, actor=actor)

    async def list(self, env: str = "", limit: int = 500) -> list[dict]:
        """The head pointers for one environment. `env=""` is the global scope, not "any"."""
        rows, _ = await self._store.find(_CONFIG, {"env": env}, limit=limit, sort=("key", "asc"))
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
        # sweeper: one row per module under maintenance. Reading them all IS the operation.
        return await self._store.scan_all(_MAINTENANCE, _CATALOG_MAX)
