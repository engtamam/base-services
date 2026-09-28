"""Retention enforcement: delete what we may no longer keep, and NOTHING else.

This code deletes real records, so the tests are weighted towards what must survive. Over-retention
is a finding; a wrong delete is unrecoverable, and there is no undo to test afterwards.
"""
from __future__ import annotations

import asyncio

import pytest

from app.config import Settings
from app.core.retention import DAY_SECONDS, RetentionEnforcer

_NOW = 1_000_000_000.0
_OK = {"INTERNAL_SERVICE_TOKEN": "x" * 40}


def run(c):
    return asyncio.run(c)


class _Clock:
    def now(self):
        return _NOW


class _Store:
    def __init__(self, data):
        self.data = {k: list(v) for k, v in data.items()}
        self.deleted = []

    async def scan_all(self, collection, limit):
        return list(self.data.get(collection, []))[:limit]

    async def delete(self, collection, key):
        self.deleted.append((collection, key))
        self.data[collection] = [r for r in self.data[collection] if r["id"] != key]


class _Holds:
    def __init__(self, held=()):
        self.held = set(held)

    async def active_for(self, tenant, subject):
        return [{"id": "hold_1"}] if (tenant, subject) in self.held else []


def _row(id, age_days, **kw):
    # A retention-eligible DSAR is TERMINAL: its clock is completed_at (compliance-001). Default to a
    # completed row aged `age_days`; a test wanting an OPEN dsar passes status="in_progress".
    ts = _NOW - age_days * DAY_SECONDS
    row = {"id": id, "tenant": "acme", "subject": "u_7", "created_at": ts,
           "status": "completed", "completed_at": ts}
    row.update(kw)
    return row


def _mk(store, rules, holds=None):
    return RetentionEnforcer(store=store, holds=holds or _Holds(), rules=rules, clock=_Clock())


def test_expired_rows_are_purged_and_fresh_ones_survive():
    store = _Store({"dsar": [_row("old", 400), _row("fresh", 10)]})
    out = run(_mk(store, {"dsar": 365}).run())
    assert store.deleted == [("dsar", "old")]
    assert out["purged"] == {"dsar": 1}


def test_an_open_dsar_is_never_purged_even_when_old(_holds=None):
    # compliance-001: an in-flight DSAR (no completed_at, non-terminal status) must NOT age out by
    # created_at — deleting it mid-flight destroys the evidence of an unfulfilled legal obligation.
    store = _Store({"dsar": [_row("stuck_open", 400, status="in_progress", completed_at=0.0)]})
    out = run(_mk(store, {"dsar": 365}).run())
    assert store.deleted == []                      # retained despite being 400 days old
    assert out.get("purged", {}) == {}


def test_a_completed_dsar_ages_from_completion_not_filing():
    # filed 400 days ago but only completed 10 days ago → inside a 365-day window → retained.
    store = _Store({"dsar": [_row("recent_complete", 400,
                                  completed_at=_NOW - 10 * DAY_SECONDS)]})
    out = run(_mk(store, {"dsar": 365}).run())
    assert store.deleted == []


def test_a_legal_hold_beats_retention():
    # Expiry is permission to delete; a hold is a prohibition. The prohibition wins.
    store = _Store({"dsar": [_row("held", 400), _row("free", 400)]})
    out = run(_mk(store, {"dsar": 365}, holds=_Holds([("acme", "u_7")])).run())
    # both rows share the subject, so BOTH are held
    assert store.deleted == []
    assert out["held_by_legal_hold"] == {"dsar": 2} and out["purged"] == {}


def test_a_collection_with_no_configured_period_is_never_touched():
    # An undecided retention must read as KEEP: deleting on a default destroys records nobody chose
    # to destroy, and unlike over-retention that cannot be undone.
    store = _Store({"dsar": [_row("ancient", 99_999)]})
    out = run(_mk(store, {}).run())
    assert store.deleted == [] and out["purged"] == {}


def test_zero_days_means_keep_not_delete_everything():
    # The dangerous misreading: 0 as "retain for zero days" would wipe the collection.
    store = _Store({"dsar": [_row("ancient", 99_999)]})
    run(_mk(store, {"dsar": 0}).run())
    assert store.deleted == []


def test_a_dsar_ages_from_when_it_closed_not_when_it_was_filed():
    # A request that took 29 days must not be deletable the day after it completes.
    store = _Store({"dsar": [
        _row("slow", 400, completed_at=_NOW - 10 * DAY_SECONDS),   # filed long ago, closed recently
        _row("quick", 400, completed_at=_NOW - 400 * DAY_SECONDS),
    ]})
    run(_mk(store, {"dsar": 365}).run())
    assert store.deleted == [("dsar", "quick")]


def test_an_old_but_recently_closed_row_does_not_hide_expired_rows_behind_it():
    # Rows are read created_at-ordered but aged by completed_at, so a naive "stop at the first
    # in-window row" would leave everything after it undeleted for ever.
    store = _Store({"dsar": [
        _row("blocker", 500, completed_at=_NOW),                    # oldest by created_at, fresh
        _row("expired", 400, completed_at=_NOW - 400 * DAY_SECONDS),
    ]})
    run(_mk(store, {"dsar": 365}).run())
    assert store.deleted == [("dsar", "expired")]


def test_rows_with_no_clock_field_are_never_purged():
    store = _Store({"dsar": [{"id": "no_clock", "tenant": "acme", "subject": "u"}]})
    run(_mk(store, {"dsar": 1}).run())
    assert store.deleted == []


def test_rows_with_no_subject_cannot_be_held_and_age_out():
    store = _Store({"processing_activities": [
        {"id": "ropa_1", "tenant": "acme", "created_at": _NOW - 400 * DAY_SECONDS}]})
    run(_mk(store, {"processing_activities": 365}, holds=_Holds([("acme", "u_7")])).run())
    assert store.deleted == [("processing_activities", "ropa_1")]


def test_the_whole_collection_is_walked():
    # An earlier version paged with a cursor and deleted as it went, which shifted the collection
    # underneath itself and skipped exactly as many rows as it deleted — every run, for ever.
    store = _Store({"dsar": [_row(f"r{i}", 400) for i in range(25)]})
    out = run(_mk(store, {"dsar": 365}).run())
    assert out["purged"] == {"dsar": 25}


def test_a_run_is_capped_so_a_huge_backlog_cannot_run_for_ever():
    store = _Store({"dsar": [_row(f"r{i}", 400) for i in range(100)]})
    enf = RetentionEnforcer(store=store, holds=_Holds(), rules={"dsar": 365}, clock=_Clock(),
                            max_per_run=30)
    out = run(enf.run())
    assert out["purged"] == {"dsar": 30}


# ── config ────────────────────────────────────────────────────────────────────
def test_rules_parse():
    assert Settings(RETENTION_RULES="dsar=365, breaches=1825", **_OK).retention_rules == {
        "dsar": 365, "breaches": 1825}


def test_no_rules_configured_purges_nothing():
    assert Settings(**_OK).retention_rules == {}


@pytest.mark.parametrize("bad", ["dsar", "dsar=abc", "dsar=-1", "=365"])
def test_a_malformed_period_is_an_error_not_a_silent_zero(bad):
    # A typo that reads as "keep" would hide a retention nobody is enforcing; a typo that reads as
    # "delete now" would be catastrophic. Both must fail loudly instead.
    with pytest.raises(ValueError):
        Settings(RETENTION_RULES=bad, **_OK).retention_rules
