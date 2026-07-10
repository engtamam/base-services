"""Unit: FlagService (CRUD, overrides, stale, provider outage), Config versioning, Maintenance."""
from __future__ import annotations

import asyncio
import time

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.entitlement.null import NullEntitlement
from app.adapters.flags.local import LocalFlags
from app.adapters.flags.null import NullFlags
from app.core.config_service import ConfigService, MaintenanceService
from app.core.flag_service import FlagService
from app.core.stores import ConfigStore, FlagStore, MaintenanceStore
from app.ports.flag_provider import ProviderUnavailable


def run(c):
    return asyncio.run(c)


def _svc(st, provider=None, entitlement=None, bus=None):
    return FlagService(store=FlagStore(st), provider=provider or LocalFlags(st),
                       entitlement=entitlement or NullEntitlement(), event_bus=bus, salt="s")


# ── FlagService ───────────────────────────────────────────────────────────────
def test_upsert_is_partial_update():
    st = MemoryDataStore()
    b = _svc(st)
    run(b.upsert(key="f", enabled=True, rollout_pct=25.0, description="d"))
    run(b.upsert(key="f", rollout_pct=75.0))                # only pct changes
    f = run(b.get("f"))
    assert f.enabled is True and f.rollout_pct == 75.0 and f.description == "d"


def test_overrides_set_and_clear():
    st = MemoryDataStore()
    b = _svc(st)
    run(b.upsert(key="f", enabled=True, rollout_pct=0))
    run(b.set_override("f", scope="user", subject="alice", value=True))
    assert run(b.evaluate("f", {"user": "alice"}))["reason"] == "user_override"
    run(b.clear_override("f", scope="user", subject="alice"))
    assert run(b.evaluate("f", {"user": "alice"}))["reason"] == "default"


def test_archive_hides_and_kills():
    st = MemoryDataStore()
    b = _svc(st)
    run(b.upsert(key="f", enabled=True))
    run(b.archive("f"))
    assert run(b.list()) == []
    assert run(b.evaluate("f", {"user": "a"}))["reason"] == "kill_switch"
    assert len(run(b.list(include_archived=True))) == 1


def test_override_on_missing_flag_returns_none():
    b = _svc(MemoryDataStore())
    assert run(b.set_override("nope", scope="user", subject="a", value=True)) is None
    assert run(b.archive("nope")) is None


def test_null_provider_turns_everything_off():
    st = MemoryDataStore()
    b = _svc(st)
    run(b.upsert(key="f", enabled=True, rollout_pct=100))
    off = _svc(st, provider=NullFlags())
    assert run(off.evaluate("f", {"user": "a"}))["reason"] == "unknown_flag"
    assert run(off.evaluate_all({"user": "a"})) == {}


def test_provider_outage_is_fail_safe():
    class Down:
        driver = "down"

        async def get(self, key):
            raise ProviderUnavailable("down")

        async def list(self, limit=500):
            raise ProviderUnavailable("down")

    b = _svc(MemoryDataStore(), provider=Down())
    assert run(b.evaluate("f", {"user": "a"}))["reason"] == "unknown_flag"   # default OFF
    assert run(b.evaluate_all({"user": "a"})) == {}


def test_entitlement_checker_error_fails_closed():
    class Boom:
        driver = "boom"

        def allowed(self, *, tenant, key):
            raise RuntimeError("billing down")

    st = MemoryDataStore()
    b = _svc(st, entitlement=Boom())
    run(b.upsert(key="f", enabled=True, rollout_pct=100, requires_entitlement="export"))
    assert run(b.evaluate("f", {"tenant": "t", "user": "a"}))["reason"] == "entitlement_denied"


def test_evaluate_touches_last_evaluated_and_stale_lists_unused():
    st = MemoryDataStore()
    b = _svc(st)
    run(b.upsert(key="used", enabled=True))
    run(b.upsert(key="unused", enabled=True))
    run(b.evaluate("used", {"user": "a"}))
    stale = [f["key"] for f in run(FlagStore(st).stale(time.time() - 1))]
    assert stale == ["unused"]                       # 'used' was just evaluated


def test_event_bus_outage_never_fails_a_write():
    class Bus:
        async def publish(self, *a, **k):
            raise RuntimeError("bus down")

    b = _svc(MemoryDataStore(), bus=Bus())
    assert run(b.upsert(key="f", enabled=True)).key == "f"


# ── ConfigService ─────────────────────────────────────────────────────────────
def test_config_versions_append_and_rollback_preserves_history():
    st = MemoryDataStore()
    cs = ConfigService(store=ConfigStore(st))
    run(cs.put("k", 100))
    run(cs.put("k", 500))
    assert run(cs.get("k")).value == 500
    assert [h["version"] for h in run(cs.history("k"))] == [2, 1]
    rb = run(cs.rollback("k", 1))
    assert rb.version == 3 and run(cs.get("k")).value == 100        # new version, old content
    assert [h["version"] for h in run(cs.history("k"))] == [3, 2, 1]  # history intact


def test_rollback_unknown_version():
    cs = ConfigService(store=ConfigStore(MemoryDataStore()))
    run(cs.put("k", 1))
    assert run(cs.rollback("k", 99)) is None


def test_env_scoped_config_wins_over_global():
    st = MemoryDataStore()
    glob = ConfigService(store=ConfigStore(st))
    prod = ConfigService(store=ConfigStore(st), env="prod")
    run(glob.put("limit", 100))
    assert run(prod.get("limit")).value == 100          # falls back to global
    run(prod.put("limit", 999))
    assert run(prod.get("limit")).value == 999          # env wins
    assert run(glob.get("limit")).value == 100          # global untouched


# ── Maintenance ───────────────────────────────────────────────────────────────
def test_maintenance_window_before_during_after():
    st = MemoryDataStore()
    ms = MaintenanceService(store=MaintenanceStore(st))
    now = time.time()
    run(ms.set(module="global", enabled=True, start=now + 60, end=now + 120))
    assert run(ms.status())["active"] is False              # scheduled, not started
    run(ms.set(module="global", enabled=True, start=now - 60, end=now + 60))
    assert run(ms.status())["active"] is True               # inside the window
    run(ms.set(module="global", enabled=True, start=now - 120, end=now - 60))
    assert run(ms.status())["active"] is False              # window passed


def test_partial_maintenance_per_module():
    st = MemoryDataStore()
    ms = MaintenanceService(store=MaintenanceStore(st))
    run(ms.set(module="reports", enabled=True, message="reindexing"))
    assert run(ms.status("reports"))["active"] is True
    assert run(ms.status("billing"))["active"] is False


def test_global_maintenance_implies_every_module():
    st = MemoryDataStore()
    ms = MaintenanceService(store=MaintenanceStore(st))
    run(ms.set(module="global", enabled=True, message="upgrade"))
    s = run(ms.status("billing"))
    assert s["active"] is True and s["module"] == "global"


def test_maintenance_disabled_is_never_active():
    ms = MaintenanceService(store=MaintenanceStore(MemoryDataStore()))
    run(ms.set(module="global", enabled=False, start=0, end=0))
    assert run(ms.status())["active"] is False
