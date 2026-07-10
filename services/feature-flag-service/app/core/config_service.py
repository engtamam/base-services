"""Versioned system configuration + maintenance mode.

Config is append-only: a put appends a version; a rollback appends a NEW version holding
the old content. History is never rewritten (auditable, like our audit-service chain).
An env-scoped entry (CONFIG_ENV) wins over the global one.
"""
from __future__ import annotations

from typing import Any

from app.core.models import ConfigEntry, Maintenance, iso
from app.core.stores import ConfigStore, MaintenanceStore


class ConfigService:
    def __init__(self, *, store: ConfigStore, env: str = "", event_bus=None, list_limit: int = 500) -> None:
        self._store = store
        self._env = env
        self._bus = event_bus
        self._limit = list_limit

    async def put(self, key: str, value: Any, *, env: str | None = None, actor: str = "") -> ConfigEntry:
        entry = await self._store.put(key, value, env=self._env if env is None else env, actor=actor)
        await self._emit("config.changed", {"key": key, "version": entry.version, "actor": actor})
        return entry

    async def get(self, key: str, *, env: str | None = None) -> ConfigEntry | None:
        return await self._store.get(key, env=self._env if env is None else env)

    async def history(self, key: str, *, env: str | None = None) -> list[dict]:
        return await self._store.history(key, env=self._env if env is None else env, limit=self._limit)

    async def rollback(self, key: str, version: int, *, env: str | None = None,
                       actor: str = "") -> ConfigEntry | None:
        entry = await self._store.rollback(key, version, env=self._env if env is None else env, actor=actor)
        if entry:
            await self._emit("config.rolled_back",
                             {"key": key, "to_version": version, "new_version": entry.version, "actor": actor})
        return entry

    async def list(self, env: str | None = None) -> list[dict]:
        return await self._store.list(self._env if env is None else env, self._limit)

    async def _emit(self, event: str, payload: dict) -> None:
        if self._bus is None:
            return
        try:
            await self._bus.publish(event, {**payload, "at": iso()})
        except Exception:
            pass


class MaintenanceService:
    """Global + per-module (partial) maintenance with scheduled windows.

    A bypass allowlist lets admins and health checks reach the system DURING maintenance —
    otherwise you cannot fix the thing you took down to fix.
    """

    def __init__(self, *, store: MaintenanceStore, event_bus=None,
                 bypass_subjects: frozenset[str] = frozenset()) -> None:
        self._store = store
        self._bus = event_bus
        self._bypass = bypass_subjects

    async def set(self, *, module: str = "global", enabled: bool = False, message: str = "",
                  start: float = 0.0, end: float = 0.0) -> Maintenance:
        m = await self._store.set(Maintenance(module=module, enabled=enabled, message=message,
                                              start=start, end=end))
        if self._bus is not None:
            try:
                await self._bus.publish("maintenance.changed", {"module": module, "enabled": enabled})
            except Exception:
                pass
        return m

    async def status(self, module: str = "global", *, subject: str = "") -> dict[str, Any]:
        """Global maintenance implies every module is down. Allowlisted subjects bypass."""
        bypassed = bool(subject) and subject in self._bypass
        glob = await self._store.get("global")
        if glob.active_now():
            return {"active": not bypassed, "bypassed": bypassed, "module": "global",
                    "message": glob.message, "start": glob.start, "end": glob.end}
        if module != "global":
            m = await self._store.get(module)
            if m.active_now():
                return {"active": not bypassed, "bypassed": bypassed, "module": module,
                        "message": m.message, "start": m.start, "end": m.end}
        return {"active": False, "bypassed": False, "module": module, "message": ""}

    async def all(self) -> list[dict]:
        return await self._store.all()
