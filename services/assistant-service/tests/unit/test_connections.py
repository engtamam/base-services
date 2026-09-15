"""AI connections — isolated per-tenant LLM lines, each priced on its own.

The load-bearing properties: a connection's usage/cost is metered against ONLY itself, prices are
integer micros (never float money), validation refuses a bad shape/auth, and invoking one runs its
OWN prompt through its OWN caller.
"""
from __future__ import annotations

import asyncio
import itertools

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.connections import (AiConnection, ConnectionError, ConnectionStore, cost_micros,
                                  invoke)


def run(c):
    return asyncio.run(c)


def _store(st=None):
    ids = (f"c{i}" for i in itertools.count(1))
    return ConnectionStore(st or MemoryDataStore(), now=lambda: "2026-09-06T00:00:00Z",
                           new_id=lambda: next(ids))


# ── cost arithmetic (integer micros, never float) ────────────────────────────────
def test_cost_is_integer_micros_per_1k_tokens():
    # 2500 in @ 100 micros/1k, 800 out @ 300 micros/1k
    assert cost_micros(2500, 800, 100, 300) == ((2500 * 100 + 999) // 1000) + ((800 * 300 + 999) // 1000)


def test_cost_is_zero_when_no_price_or_no_tokens():
    assert cost_micros(0, 0, 100, 300) == 0
    assert cost_micros(1000, 1000, 0, 0) == 0


# ── store CRUD + validation ──────────────────────────────────────────────────────
def test_create_and_get_roundtrip():
    s = _store()
    c = run(s.create(tenant="t1", name="support-bot", url="https://p/v1", shape="chat",
                     model="gpt", input_price_micros=100, output_price_micros=300))
    got = run(s.get("t1", c.id))
    assert got.name == "support-bot" and got.input_price_micros == 100 and got.currency == "SAR"


def test_anthropic_shape_is_accepted_per_connection():
    # A connection can pick shape="anthropic" (Claude) on its own — per-connection, not env-wide.
    s = _store()
    c = run(s.create(tenant="t1", name="claude", url="https://p/anthropic:rawPredict",
                     shape="anthropic", auth="vertex", model="claude-sonnet-4"))
    assert run(s.get("t1", c.id)).shape == "anthropic"


def test_bad_shape_and_auth_are_refused():
    s = _store()
    with pytest.raises(ConnectionError):
        run(s.create(tenant="t1", name="x", url="u", shape="stream"))
    with pytest.raises(ConnectionError):
        run(s.create(tenant="t1", name="x", url="u", auth="oauth-something"))
    with pytest.raises(ConnectionError):
        run(s.create(tenant="t1", name="x", url="u", input_price_micros=-1))


def test_list_is_tenant_scoped():
    st = MemoryDataStore()
    s = _store(st)   # ONE store so ids are globally unique (real new_id is a uuid), as the bare-cid
    run(s.create(tenant="t1", name="a", url="u"))   # key scheme requires — not reused c1/c1 per call
    run(s.create(tenant="t2", name="b", url="u"))
    rows = run(s.list("t1"))
    assert len(rows) == 1 and rows[0]["name"] == "a"


def test_delete_removes_the_connection_and_its_usage():
    st = MemoryDataStore()
    s = _store(st)
    c = run(s.create(tenant="t1", name="a", url="u"))
    run(s.record_usage(tenant="t1", cid=c.id, tokens_in=10, tokens_out=5, cost=7))
    assert run(s.delete("t1", c.id)) is True
    assert run(s.get("t1", c.id)) is None
    assert run(s.usage("t1", c.id))["calls"] == 0     # usage cleared too
    assert run(s.delete("t1", c.id)) is False          # idempotent


def test_record_usage_never_loses_a_concurrent_increment():
    """Metering is atomic: N in-flight calls on one connection all land, so cost is never under-billed.

    The old get()+put() pair lost increments under concurrency (both read N, both write N+1), which
    silently under-counted tokens and undercharged the tenant — a money leak that grows with traffic.
    """
    st = MemoryDataStore()
    s = _store(st)
    c = run(s.create(tenant="t1", name="a", url="u"))

    async def hammer():
        await asyncio.gather(*[
            s.record_usage(tenant="t1", cid=c.id, tokens_in=2, tokens_out=3, cost=5)
            for _ in range(40)])
    run(hammer())
    u = run(s.usage("t1", c.id))
    assert u["calls"] == 40 and u["tokens_in"] == 80 and u["tokens_out"] == 120 and u["cost_micros"] == 200


# ── invoke: isolation + metering ─────────────────────────────────────────────────
def test_invoke_runs_the_connections_own_prompt_and_meters_only_it():
    st = MemoryDataStore()
    s = _store(st)
    conn = run(s.create(tenant="t1", name="a", url="u", system_prompt="You are line A",
                        input_price_micros=100, output_price_micros=300))
    other = run(s.create(tenant="t1", name="b", url="u"))
    seen = {}

    async def caller(system, user, max_tokens):
        seen.update(system=system, user=user)
        return "hello", 2000, 1000      # tokens_in, tokens_out

    out = run(invoke(conn, prompt="hi", caller=caller, store=s))
    assert seen["system"] == "You are line A" and seen["user"] == "hi"
    assert out["text"] == "hello" and out["tokens_in"] == 2000
    assert out["cost_micros"] == cost_micros(2000, 1000, 100, 300) and out["currency"] == "SAR"
    # only THIS connection was metered; the other stays at zero
    assert run(s.usage("t1", conn.id))["calls"] == 1 and run(s.usage("t1", conn.id))["tokens_in"] == 2000
    assert run(s.usage("t1", other.id))["calls"] == 0


def test_usage_accumulates_across_calls():
    st = MemoryDataStore()
    s = _store(st)
    conn = run(s.create(tenant="t1", name="a", url="u", input_price_micros=10, output_price_micros=10))

    async def caller(system, user, max_tokens):
        return "x", 1000, 1000

    run(invoke(conn, prompt="1", caller=caller, store=s))
    run(invoke(conn, prompt="2", caller=caller, store=s))
    u = run(s.usage("t1", conn.id))
    assert u["calls"] == 2 and u["tokens_in"] == 2000 and u["cost_micros"] == 40


# ── bare-cid key scheme: IDOR guard, key==id, re-key migration (backup coverage) ──
def test_reads_are_tenant_guarded_against_idor():
    """Now keyed by the global cid, a cross-tenant read must be refused explicitly (the composite key
    used to prevent it structurally)."""
    st = MemoryDataStore()
    s = _store(st)
    c = run(s.create(tenant="owner", name="a", url="u"))
    run(s.record_usage(tenant="owner", cid=c.id, tokens_in=10, tokens_out=5, cost=7))
    # another tenant naming the real cid gets nothing — not the connection, not the usage
    assert run(s.get("attacker", c.id)) is None
    assert run(s.usage("attacker", c.id))["calls"] == 0
    # the real owner still sees both
    assert run(s.get("owner", c.id)).name == "a"
    assert run(s.usage("owner", c.id))["calls"] == 1


def test_rows_are_stored_under_the_bare_cid_key():
    st = MemoryDataStore()
    s = _store(st)
    c = run(s.create(tenant="t1", name="a", url="u"))
    run(s.record_usage(tenant="t1", cid=c.id, tokens_in=1, tokens_out=1, cost=1))
    # datastore key == the row's own id (the key==id invariant a backup restore relies on)
    assert run(st.get("ai_connections", c.id))["id"] == c.id
    assert run(st.get("ai_connection_usage", c.id))["id"] == c.id


def test_migrate_keys_rekeys_legacy_rows_idempotently():
    st = MemoryDataStore()
    s = _store(st)
    # simulate legacy rows written under the OLD composite key (tenant|cid), no id on usage
    run(st.put("ai_connections", "t1|old1",
               {"id": "old1", "tenant": "t1", "name": "legacy", "url": "u"}))
    run(st.put("ai_connection_usage", "t1|old1",
               {"tenant": "t1", "connection_id": "old1", "calls": 3, "cost_micros": 42}))

    first = run(s.migrate_keys())
    assert first["moved"] == 2
    # now reachable by the bare cid, tenant-guarded, and keyed by id
    assert run(s.get("t1", "old1")).name == "legacy"
    assert run(s.usage("t1", "old1"))["calls"] == 3
    assert run(st.get("ai_connections", "old1"))["id"] == "old1"
    assert run(st.get("ai_connection_usage", "old1"))["id"] == "old1"
    assert run(st.get("ai_connections", "t1|old1")) is None      # composite duplicate removed
    # idempotent: a second run moves nothing
    assert run(s.migrate_keys())["moved"] == 0


def test_migrate_keys_skips_a_row_without_identity():
    st = MemoryDataStore()
    s = _store(st)
    run(st.put("ai_connections", "junk", {"name": "no id, no tenant"}))
    assert run(s.migrate_keys())["skipped"] >= 1


def test_connections_and_usage_survive_a_backup_roundtrip():
    from app.core import backup as B
    from app.backup_manifest import BACKUP
    st = MemoryDataStore()
    s = _store(st)
    c = run(s.create(tenant="t1", name="a", url="u"))
    run(s.record_usage(tenant="t1", cid=c.id, tokens_in=100, tokens_out=50, cost=9))

    seal, unseal = B.make_sealer("backup-key")
    archive = run(B.export(st, BACKUP, service="assistant-service", version="1.0.0",
                           taken_at="2026-09-15T00:00:00Z", seal=seal, tenant="", scan_cap=1000))
    dst = MemoryDataStore()
    run(B.import_archive(dst, archive, unseal=unseal))
    s2 = ConnectionStore(dst, now=lambda: "x", new_id=lambda: "n")
    # each row restored under its own key — not collapsed under a single None id
    assert run(s2.get("t1", c.id)).name == "a"
    assert run(s2.usage("t1", c.id))["cost_micros"] == 9
