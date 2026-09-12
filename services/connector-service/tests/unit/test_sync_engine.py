"""SyncEngine: watermark-after-commit, per-record idempotency, partial→DLQ, echo loop, circuit."""
from __future__ import annotations

import asyncio

from app.adapters.cache.memory import MemoryCache
from app.adapters.clock.manual import ManualClock
from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.provider.recording import RecordingConnector
from app.core.models import Connection
from app.core.stores import ConnectionStore, DlqStore, RunStore, SeenStore
from app.core.sync_engine import ORIGIN_FIELD, SyncEngine
from app.core.token_manager import TokenManager
from app.ports.connector_provider import (CredentialInvalid, Page, ProviderUnavailable,
                                          RateLimited, Tokens)

CFG = dict(batch_size=100, max_attempts=5, retry_base_seconds=5, retry_max_seconds=3600,
           retry_jitter=True, circuit_threshold=2, circuit_reset_seconds=300)
SEAL = "k" * 32


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
    return se, conns, prov, DlqStore(st), clk


IDMAP = {"id": {"op": "path", "value": "id"}}


# ── watermark safety (silent data loss) ───────────────────────────────────────
def test_clean_batch_advances_the_watermark():
    se, conns, prov, _, _ = _env()
    prov.pages["c"] = [Page(records=[{"id": "1"}, {"id": "2"}], next_cursor="CUR2")]
    r = run(se.pull(connection_id="cn_1", resource="c", spec={}, field_map=IDMAP))
    assert r["status"] == "succeeded" and r["watermark_after"] == "CUR2"
    assert run(conns.get("cn_1")).watermarks["c"] == "CUR2"


def test_a_map_failure_dead_letters_and_still_advances_the_watermark():
    """A permanently-unmappable record must NOT freeze progress (poison-pill). It is durably
    dead-lettered AND its idempotency key reserved, so advancing the watermark cannot lose it — and
    NOT advancing (the old behaviour) re-fetched the same page forever and re-DLQ'd it every run."""
    import app.core.sync_engine as SE
    original = SE._mapping.apply

    def boom(mapping, record):
        if record.get("id") == "2":
            raise RuntimeError("bad record")
        return original(mapping, record)

    se, conns, prov, dlq, _ = _env()
    SE._mapping.apply = boom
    try:
        prov.pages["c"] = [Page(records=[{"id": "1"}, {"id": "2"}, {"id": "3"}], next_cursor="CUR2")]
        r = run(se.pull(connection_id="cn_1", resource="c", spec={}, field_map=IDMAP))
    finally:
        SE._mapping.apply = original
    assert r["status"] == "partial" and r["records_failed"] == 1
    assert r["watermark_after"] == "CUR2"                          # advanced past the poison record
    assert run(conns.get("cn_1")).watermarks.get("c", "") == "CUR2"
    assert [d["record"]["id"] for d in run(dlq.list("t1"))] == ["2"]   # dead-lettered, not dropped
    # the healthy connection is NOT circuit-tripped by a client-side map failure
    assert run(conns.get("cn_1")).consecutive_failures == 0


def test_partial_run_is_never_reported_as_success():
    se, conns, prov, _, _ = _env()
    import app.core.sync_engine as SE
    original = SE._mapping.apply
    SE._mapping.apply = lambda m, r: (_ for _ in ()).throw(RuntimeError("x"))
    try:
        prov.pages["c"] = [Page(records=[{"id": "1"}], next_cursor="C")]
        r = run(se.pull(connection_id="cn_1", resource="c", spec={}, field_map=IDMAP))
    finally:
        SE._mapping.apply = original
    assert r["status"] != "succeeded" and r["error_rate"] == 1.0


# ── per-record idempotency ────────────────────────────────────────────────────
def test_replayed_records_are_not_processed_twice():
    se, _, prov, _, _ = _env()
    prov.pages["c"] = [Page(records=[{"id": "1"}, {"id": "2"}], next_cursor="")]
    first = run(se.pull(connection_id="cn_1", resource="c", spec={}, field_map=IDMAP))
    prov.pages["c"] = [Page(records=[{"id": "1"}, {"id": "2"}], next_cursor="")]
    second = run(se.pull(connection_id="cn_1", resource="c", spec={}, field_map=IDMAP))
    assert first["records_out"] == 2 and second["records_out"] == 0


def test_push_is_idempotent_across_retries():
    se, _, prov, _, _ = _env()
    run(se.push(connection_id="cn_1", resource="c", records=[{"id": "9"}], spec={}))
    run(se.push(connection_id="cn_1", resource="c", records=[{"id": "9"}], spec={}))
    assert len(prov.pushed) == 1        # never double-written


# ── echo-loop guard ───────────────────────────────────────────────────────────
def test_records_pulled_from_a_provider_are_never_pushed_back():
    se, _, prov, _, _ = _env()
    out = run(se.push(connection_id="cn_1", resource="c",
                      records=[{"id": "1", ORIGIN_FIELD: "rec"}, {"id": "2"}], spec={}))
    assert out["records_out"] == 1 and out["skipped"] == 1
    assert [p["record"]["id"] for p in prov.pushed] == ["2"]


def test_origin_marker_is_stripped_before_pushing():
    se, _, prov, _, _ = _env()
    run(se.push(connection_id="cn_1", resource="c", records=[{"id": "5", ORIGIN_FIELD: "other"}],
                spec={}))
    assert ORIGIN_FIELD not in prov.pushed[0]["record"]


def test_pulled_records_carry_the_origin_marker():
    se, _, prov, _, _ = _env()
    prov.pages["c"] = [Page(records=[{"id": "1"}], next_cursor="")]
    r = run(se.pull(connection_id="cn_1", resource="c", spec={}, field_map=IDMAP))
    assert r["records"][0][ORIGIN_FIELD] == "rec"


# ── partial push ──────────────────────────────────────────────────────────────
def test_push_partial_dead_letters_the_failures_and_keeps_the_successes():
    class Flaky(RecordingConnector):
        def push(self, *, access_token, resource, record, idempotency_key, spec):
            if record.get("id") == "bad":
                raise ProviderUnavailable("boom")
            return super().push(access_token=access_token, resource=resource, record=record,
                                idempotency_key=idempotency_key, spec=spec)

    se, _, prov, dlq, _ = _env(provider=Flaky())
    out = run(se.push(connection_id="cn_1", resource="c",
                      records=[{"id": "ok1"}, {"id": "bad"}, {"id": "ok2"}], spec={}))
    assert out["status"] == "partial" and out["records_out"] == 2 and out["records_failed"] == 1
    assert [d["record"]["id"] for d in run(dlq.list("t1"))] == ["bad"]


# ── provider errors / rate limits / circuit ────────────────────────────────────
def test_rate_limit_returns_retry_in_from_provider_header():
    class Limited(RecordingConnector):
        def fetch(self, **k):
            raise RateLimited("429", retry_after=90)

    se, _, _, _, _ = _env(provider=Limited())
    r = run(se.pull(connection_id="cn_1", resource="c", spec={}))
    assert r["status"] == "failed" and r["retry_in"] == 90.0     # obeyed, not our own curve


def test_circuit_opens_after_repeated_failures_and_blocks_further_syncs():
    class Down(RecordingConnector):
        def fetch(self, **k):
            raise ProviderUnavailable("down")

    se, _, _, _, _ = _env(provider=Down())
    run(se.pull(connection_id="cn_1", resource="c", spec={}))
    run(se.pull(connection_id="cn_1", resource="c", spec={}))    # threshold=2 → open
    blocked = run(se.pull(connection_id="cn_1", resource="c", spec={}))
    assert blocked["reason"] == "circuit_open"


def test_credential_invalid_fails_the_run_not_the_process():
    class Bad(RecordingConnector):
        def fetch(self, **k):
            raise CredentialInvalid("401")

    se, _, _, _, _ = _env(provider=Bad())
    r = run(se.pull(connection_id="cn_1", resource="c", spec={}))
    assert r["status"] == "failed" and r["error"] == "CredentialInvalid"


def test_unknown_connection():
    se, _, _, _, _ = _env()
    assert run(se.pull(connection_id="nope", resource="c", spec={}))["reason"] == "NOT_FOUND"


# ── monitoring ────────────────────────────────────────────────────────────────
def test_health_reports_error_rate_and_circuit():
    se, _, prov, _, _ = _env()
    prov.pages["c"] = [Page(records=[], next_cursor="")]
    run(se.pull(connection_id="cn_1", resource="c", spec={}))
    h = run(se.health("cn_1"))
    assert h["circuit_state"] == "closed" and h["sync_count"] == 1 and h["error_rate"] == 0.0
    assert run(se.health("ghost")) is None
