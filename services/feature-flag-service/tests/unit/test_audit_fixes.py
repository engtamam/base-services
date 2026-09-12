"""Regression tests for the 2026-09-12 fleet-audit findings on feature-flag-service (#03, #04).

(#01 offload is enforced by safety:async-blocking; #02 user-scope guard and #05 evaluate scoping are
covered at the API level in test_flags_api_audit.py.)
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.stores import ConfigStore, FlagStore
from app.core.flag_service import FlagService


def run(c):
    return asyncio.run(c)


class _NullEnt:
    def allowed(self, *, tenant, key):
        return True


class _Local:
    def __init__(self, st):
        self._s = FlagStore(st)

    async def get(self, key):
        f = await self._s.get(key)
        return f.to_dict() if f else None

    async def list(self, limit):
        return await self._s.list(limit, True)


def _svc(st):
    return FlagService(store=FlagStore(st), provider=_Local(st), entitlement=_NullEnt(), salt="s")


# ── 03: concurrent config puts do not lose a version ──────────────────────────
def test_concurrent_config_puts_keep_every_version():
    st = MemoryDataStore()
    cfg = ConfigStore(st)

    async def _race():
        return await asyncio.gather(
            cfg.put("k", "a", actor="x"), cfg.put("k", "b", actor="y"),
            cfg.put("k", "c", actor="z"))

    entries = run(_race())
    versions = sorted(e.version for e in entries)
    assert versions == [1, 2, 3]                       # three distinct versions, none lost
    hist = run(cfg.history("k", limit=10))
    assert len({h["version"] for h in hist}) == 3      # all three durably in history


# ── 04: concurrent flag writes do not clobber each other ──────────────────────
def test_concurrent_flag_writes_both_land():
    st = MemoryDataStore()
    svc = _svc(st)
    run(svc.upsert(key="f", enabled=True, actor="init"))

    async def _race():
        return await asyncio.gather(
            svc.set_override("f", scope="user", subject="u1", value=True, actor="a"),
            svc.upsert(key="f", description="edited", actor="b"))

    run(_race())
    flag = run(svc.get("f"))
    # both the concurrent override AND the concurrent description edit survived (no whole-doc clobber)
    assert flag.user_overrides.get("u1") is True
    assert flag.description == "edited"
