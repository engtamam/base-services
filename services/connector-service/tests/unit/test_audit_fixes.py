"""Regression tests for the 2026-09-12 fleet-audit findings on connector-service (#01, #05).

(#02 poison-pill and #06 circuit are covered in test_sync_engine.py; #03/#04 blocking-offload are
enforced by the safety:async-blocking gate; #07 is a delivery-contract decision surfaced to Tamam.)
"""
from __future__ import annotations

import asyncio

from app.adapters.cache.memory import MemoryCache
from app.adapters.clock.manual import ManualClock
from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.provider.recording import RecordingConnector
from app.core.models import Connection
from app.core.stores import ConnectionStore, DlqStore, RunStore, SeenStore
from app.core.sync_engine import SyncEngine
from app.core.token_manager import TokenManager
from app.ports.connector_provider import ProviderUnavailable, Tokens

CFG = dict(batch_size=100, max_attempts=5, retry_base_seconds=5, retry_max_seconds=3600,
           retry_jitter=True, circuit_threshold=2, circuit_reset_seconds=300)
SEAL = "k" * 32
IDMAP = {"id": {"op": "path", "value": "id"}}


def run(c):
    return asyncio.run(c)


def _env(provider=None):
    st = MemoryDataStore()
    clk = ManualClock(1000.0)
    prov = provider or RecordingConnector()
    conns = ConnectionStore(st)
    tm = TokenManager(connections=conns, provider=prov, clock=clk, seal_key=SEAL, cache=MemoryCache())
    conn = Connection(id="cn_1", tenant="t1", provider="rec")
    run(conns.save(conn))
    run(tm.store_tokens(conn, Tokens(access_token="A", refresh_token="R", expires_in=99999),
                        invalidate=True))
    se = SyncEngine(connections=conns, runs=RunStore(st), dlq=DlqStore(st), seen=SeenStore(st),
                    tokens=tm, provider=prov, clock=clk, config=CFG)
    return se, conns, prov, st


# ── 01: id-less records must not collapse to one idempotency key ───────────────
def test_idless_records_are_not_collapsed_and_dropped():
    se, conns, prov, st = _env()
    # three records with NO id/vid/_id, all distinct content — the old key was `cn:res:` for all,
    # so only the first pushed and the rest were silently skipped.
    recs = [{"name": "a"}, {"name": "b"}, {"name": "c"}]
    r = run(se.push(connection_id="cn_1", resource="c", records=recs, spec={}, field_map=IDMAP))
    assert r["records_out"] == 3 and r["skipped"] == 0
    assert len(prov.pushed) == 3
    # and a genuine duplicate of one still dedupes (same content → same hash key)
    r2 = run(se.push(connection_id="cn_1", resource="c", records=[{"name": "a"}], spec={},
                     field_map=IDMAP))
    assert r2["skipped"] == 1 and len(prov.pushed) == 3


# ── 05: a failed push RELEASES the reservation so a retry can resend ───────────
def test_failed_push_is_resendable_not_lost():
    class FlakyProvider(RecordingConnector):
        def __init__(self):
            super().__init__()
            self.fail_next = True

        def push(self, *, access_token, resource, record, idempotency_key, spec):
            if self.fail_next:
                self.fail_next = False
                raise ProviderUnavailable("down")
            return super().push(access_token=access_token, resource=resource, record=record,
                                 idempotency_key=idempotency_key, spec=spec)

    se, conns, prov, st = _env(provider=FlakyProvider())
    rec = [{"id": "x1"}]
    r1 = run(se.push(connection_id="cn_1", resource="c", records=rec, spec={}, field_map=IDMAP))
    assert r1["records_failed"] == 1 and len(prov.pushed) == 0     # first attempt failed, DLQ'd
    # a retry of the SAME record must resend (the reservation was released), not skip it as 'seen'
    r2 = run(se.push(connection_id="cn_1", resource="c", records=rec, spec={}, field_map=IDMAP))
    assert r2["records_out"] == 1 and len(prov.pushed) == 1


# ── 05: concurrent push of the same record sends exactly once ──────────────────
def test_concurrent_push_of_same_record_sends_once():
    se, conns, prov, st = _env()
    rec = [{"id": "dup1"}]

    async def _race():
        return await asyncio.gather(
            se.push(connection_id="cn_1", resource="c", records=rec, spec={}, field_map=IDMAP),
            se.push(connection_id="cn_1", resource="c", records=rec, spec={}, field_map=IDMAP))

    run(_race())
    assert len(prov.pushed) == 1                                    # atomic reserve prevented a double send
