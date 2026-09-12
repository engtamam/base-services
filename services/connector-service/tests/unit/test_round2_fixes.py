"""Round-2 fixes (connector-service). Maps to docs/audits/round2/connector-service.json."""
from __future__ import annotations

import asyncio

from app.adapters.cache.memory import MemoryCache
from app.adapters.clock.manual import ManualClock
from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.provider.recording import RecordingConnector as _RC
from app.core import token_manager as _tm
from app.core.models import Connection
from app.core.stores import ConnectionStore, DlqStore, RunStore, SeenStore
from app.core.sync_engine import SyncEngine
from app.core.token_manager import TokenManager
from app.ports.connector_provider import Page as _Page, Tokens

CFG = dict(batch_size=100, max_attempts=5, retry_base_seconds=5, retry_max_seconds=3600,
           retry_jitter=True, circuit_threshold=2, circuit_reset_seconds=300)
SEAL = "k" * 32
IDMAP = {"id": {"op": "path", "value": "id"}}


def run(c):
    return asyncio.run(c)


def _env(provider=None):
    st = MemoryDataStore()
    clk = ManualClock(1000.0)
    prov = provider or _RC()
    conns = ConnectionStore(st)
    tm = TokenManager(connections=conns, provider=prov, clock=clk, seal_key=SEAL, cache=MemoryCache())
    conn = Connection(id="cn_1", tenant="t1", provider="rec")
    run(conns.save(conn))
    run(tm.store_tokens(conn, Tokens(access_token="A", refresh_token="R", expires_in=99999),
                        invalidate=True))
    se = SyncEngine(connections=conns, runs=RunStore(st), dlq=DlqStore(st), seen=SeenStore(st),
                    tokens=tm, provider=prov, clock=clk, config=CFG)
    return se, conns, prov, st


class _CursorProvider(_RC):
    def __init__(self):
        super().__init__()
        self.by_cursor: dict[str, _Page] = {}

    def fetch(self, *, access_token, resource, cursor, limit, spec):
        return self.by_cursor.get(cursor, _Page(records=[], next_cursor=""))


# r2-01 — the terminal tail stops re-delivering once its synthetic ack token is acked.
def test_terminal_tail_stops_redelivering_after_ack():
    prov = _CursorProvider()
    prov.by_cursor[""] = _Page(records=[{"id": "1"}], next_cursor="CUR2")
    prov.by_cursor["CUR2"] = _Page(records=[{"id": "3"}], next_cursor="")   # terminal page
    se, conns, _, _ = _env(provider=prov)

    run(se.pull(connection_id="cn_1", resource="c", spec={}, field_map=IDMAP, at_least_once=True))
    r2 = run(se.pull(connection_id="cn_1", resource="c", spec={}, field_map=IDMAP,
                     at_least_once=True, ack_cursor="CUR2"))
    assert [x["id"] for x in r2["records"]] == ["3"]
    token = r2["cursor"]
    assert token and token != ""                       # a NON-EMPTY ack token even for the terminal page

    # unacked re-pull still re-delivers the tail (at-least-once preserved)
    r3 = run(se.pull(connection_id="cn_1", resource="c", spec={}, field_map=IDMAP, at_least_once=True))
    assert [x["id"] for x in r3["records"]] == ["3"]

    # ack the terminal token → the tail is committed and no longer re-delivered
    r4 = run(se.pull(connection_id="cn_1", resource="c", spec={}, field_map=IDMAP,
                     at_least_once=True, ack_cursor=token))
    assert r4["records"] == []                          # was: re-delivered forever


# r2-02 — a durably-committed (poison/acked) key is skipped, not re-processed/re-DLQ'd.
def test_committed_key_is_skipped_in_alo():
    prov = _CursorProvider()
    prov.by_cursor[""] = _Page(records=[{"id": "1"}, {"id": "2"}], next_cursor="")
    se, conns, _, st = _env(provider=prov)
    # pre-commit record 1's key (as a poison/acked marker would)
    key1 = se._idem_key("cn_1", "c", {"id": "1"})
    run(se._seen.reserve(key1))
    r = run(se.pull(connection_id="cn_1", resource="c", spec={}, field_map=IDMAP, at_least_once=True))
    assert [x["id"] for x in r["records"]] == ["2"]     # 1 skipped (committed), 2 delivered


# r2-03 — a forged ack_cursor that was never issued does not move the watermark.
def test_forged_ack_cursor_is_ignored():
    prov = _CursorProvider()
    prov.by_cursor[""] = _Page(records=[{"id": "1"}], next_cursor="CUR2")
    se, conns, _, _ = _env(provider=prov)
    run(se.pull(connection_id="cn_1", resource="c", spec={}, field_map=IDMAP, at_least_once=True))
    # attacker sends a cursor we never issued
    run(se.pull(connection_id="cn_1", resource="c", spec={}, field_map=IDMAP,
                at_least_once=True, ack_cursor="ATTACKER-CONTROLLED"))
    assert run(conns.get("cn_1")).watermarks.get("c", "") == ""   # not jumped to a forged position


# r2-07 — the per-connection refresh lock is evicted on disconnect.
def test_evict_lock_removes_the_connection_lock():
    _tm._LOCKS["cn_x"] = asyncio.Lock()
    _tm.evict_lock("cn_x")
    assert "cn_x" not in _tm._LOCKS
    _tm.evict_lock("cn_x")                              # idempotent
