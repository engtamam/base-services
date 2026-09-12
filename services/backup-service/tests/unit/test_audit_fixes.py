"""Regression tests for the 2026-09-12 fleet-audit findings on backup-service (#01, #03, #04)."""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.internal_http import TargetUnreachable, _check_host
from app.core.runs import RunStore


def run(c):
    return asyncio.run(c)


# ── 01: recent() returns the NEWEST runs, not the oldest ──────────────────────
def test_recent_returns_the_newest_run():
    st = MemoryDataStore()
    store = RunStore(st)
    # ids ascend with time (run_<ts>); insert oldest..newest
    for i in range(5):
        run(store.record({"id": f"run_2026010{i}", "started_at": f"2026-01-0{i}T00:00:00Z"}))
    latest = run(store.recent(1))
    assert len(latest) == 1
    assert latest[0]["id"] == "run_20260104"        # newest, not the oldest run_20260100


# ── 03: egress is fail-CLOSED ─────────────────────────────────────────────────
def test_egress_refused_when_allowlist_empty():
    with pytest.raises(TargetUnreachable):
        _check_host("https://anything.example/x", frozenset())


def test_egress_blocks_metadata_even_if_allowlisted():
    with pytest.raises(TargetUnreachable):
        _check_host("http://169.254.169.254/latest/", frozenset({"169.254.169.254"}))


def test_egress_rejects_non_http_scheme():
    with pytest.raises(TargetUnreachable):
        _check_host("file:///etc/passwd", frozenset({"x"}))


def test_egress_allows_an_allowlisted_host():
    assert _check_host("https://backup-target.svc/x", frozenset({"backup-target.svc"})) \
        == "backup-target.svc"


def test_egress_rejects_a_host_not_on_the_allowlist():
    with pytest.raises(TargetUnreachable):
        _check_host("https://evil.example/x", frozenset({"backup-target.svc"}))
