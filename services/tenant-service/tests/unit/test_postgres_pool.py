"""The postgres adapter's pool sizing and its capacity-vs-down diagnosis (#580).

No real database: a stand-in `asyncpg` lets the lazy import succeed, and a fake pool drives the
health probe down each branch. What this pins:
  * the pool is sized from the constructor args (min defaults to 1 — LAZY, not asyncpg's eager 10),
    so a pod is Ready on one connection and the fleet cannot silently pin 10×services;
  * max is clamped to never fall below min (a misconfig cannot produce min>max);
  * readiness distinguishes a FULL postgres ("pool exhausted (max_connections reached)") from an
    unreachable one ("down") — the difference between a one-line diagnosis and an hour on the wrong
    thing.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types

import pytest

# Stand-in so `load("asyncpg", ...)` in the adapter succeeds WITHOUT the real driver installed — but
# ONLY then. A bare module-level `setdefault` poisoned the whole pytest SESSION: this unit module is
# imported during collection, before anything imports asyncpg, so the stub (which has no
# `create_pool`) became sys.modules['asyncpg'] for every later test — and the postgres CONTRACT test,
# which needs the REAL create_pool, then ERRORed. That was the "flaky" postgres-contract failure. So
# stub only when asyncpg is genuinely absent; in CI, where it is installed for the contract, leave it.
if importlib.util.find_spec("asyncpg") is None:
    sys.modules.setdefault("asyncpg", types.ModuleType("asyncpg"))

from app.adapters.datastore import postgres as pg  # noqa: E402


def _ds(pool=None, **kw):
    ds = pg.PostgresDataStore("postgresql://x/y", **kw)
    if pool is not None:
        ds._pool = pool          # skip _ensure so no real create_pool runs
    return ds


class _Con:
    async def execute(self, *a):
        return "SELECT 1"


class _Acq:
    def __init__(self, exc):
        self._exc = exc

    async def __aenter__(self):
        if self._exc:
            raise self._exc
        return _Con()

    async def __aexit__(self, *a):
        return False


class _Pool:
    def __init__(self, exc=None):
        self._exc = exc

    def acquire(self, timeout=None):
        return _Acq(self._exc)


# ── sizing ────────────────────────────────────────────────────────────────────
def test_defaults_are_lazy_not_eager():
    ds = _ds()
    assert ds._pool_min == 1          # NOT asyncpg's eager 10
    assert ds._pool_max == 5


def test_max_is_clamped_never_below_min():
    ds = _ds(pool_min=8, pool_max=2)
    assert ds._pool_max == 8          # a misconfig cannot yield min>max


def test_params_are_captured():
    ds = _ds(pool_min=2, pool_max=9, pool_timeout=7.0, pool_max_inactive_lifetime=120.0)
    assert (ds._pool_min, ds._pool_max, ds._pool_timeout, ds._pool_max_inactive) == (2, 9, 7.0, 120.0)


# ── capacity classifier ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("exc", [
    asyncio.TimeoutError(),
    TimeoutError(),
    Exception("FATAL: sorry, too many clients already"),
])
def test_capacity_errors_are_recognised(exc):
    assert pg._is_capacity_error(exc) is True


def test_sqlstate_53300_is_capacity():
    class E(Exception):
        sqlstate = "53300"
    assert pg._is_capacity_error(E("x")) is True


def test_an_unreachable_db_is_not_capacity():
    assert pg._is_capacity_error(ConnectionRefusedError("refused")) is False


# ── readiness diagnosis ─────────────────────────────────────────────────────────
def test_health_state_ok():
    assert asyncio.run(_ds(_Pool()).health_state()) == "ok"


def test_health_state_names_a_full_pool_distinctly_from_down():
    full = _ds(_Pool(Exception("FATAL: too many clients already")))
    down = _ds(_Pool(ConnectionRefusedError("refused")))
    assert asyncio.run(full.health_state()) == "pool exhausted (max_connections reached)"
    assert asyncio.run(down.health_state()) == "down"


def test_health_bool_still_works_for_bool_callers():
    assert asyncio.run(_ds(_Pool()).health()) is True
    assert asyncio.run(_ds(_Pool(ConnectionRefusedError("x"))).health()) is False
