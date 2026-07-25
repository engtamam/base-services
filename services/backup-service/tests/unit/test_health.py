"""Backup health — the alarm on the three ways a backup quietly stops being real.

The load-bearing tests: an unverified backup does NOT count as protection (46.4 made observable), a
schedule that stopped shows up as STALE, a never-backed-up service is CRITICAL, and a recent miss on
top of an older good backup is DEGRADED, not a false all-clear and not a false emergency.
"""
from __future__ import annotations

from app.core.health import CRITICAL, DEGRADED, HEALTHY, evaluate

NOW = "2026-07-25T12:00:00Z"


def _run(run_id, started_at, results):
    return {"id": run_id, "started_at": started_at, "status": "complete", "results": results}


def _r(service, *, status="ok", verified=True):
    return {"service": service, "status": status, "verified": verified,
            "archive_key": f"{service}.json", "row_counts": {"x": 1}}


def _targets(*names):
    return [{"service": n, "enabled": True} for n in names]


def _by_service(report):
    return {t["service"]: t for t in report["targets"]}


def test_a_recent_verified_backup_is_healthy():
    report = evaluate(targets=_targets("billing"),
                      runs=[_run("r1", "2026-07-25T02:00:00Z", [_r("billing")])],
                      now_iso=NOW, stale_after_seconds=172_800)
    assert report["status"] == HEALTHY
    assert report["alerts"] == []


def test_a_never_backed_up_target_is_critical():
    report = evaluate(targets=_targets("wallet"), runs=[], now_iso=NOW, stale_after_seconds=172_800)
    t = _by_service(report)["wallet"]
    assert t["status"] == CRITICAL and "never backed up" in t["reason"]
    assert report["status"] == CRITICAL and report["alerts"][0]["service"] == "wallet"


def test_an_unverified_backup_does_not_count_as_protection():
    """The 46.4 rule, made observable: it exists, but was never proven restorable, so it is CRITICAL
    exactly as if it were absent."""
    report = evaluate(
        targets=_targets("billing"),
        runs=[_run("r1", "2026-07-25T02:00:00Z", [_r("billing", verified=False)])],
        now_iso=NOW, stale_after_seconds=172_800)
    t = _by_service(report)["billing"]
    assert t["status"] == CRITICAL and "no verified backup" in t["reason"]


def test_a_stale_backup_is_critical():
    """No verified backup newer than tolerance — the schedule may have silently stopped."""
    report = evaluate(
        targets=_targets("billing"),
        runs=[_run("r1", "2026-07-20T02:00:00Z", [_r("billing")])],   # >48h before NOW
        now_iso=NOW, stale_after_seconds=172_800)
    t = _by_service(report)["billing"]
    assert t["status"] == CRITICAL and "past the" in t["reason"]


def test_a_recent_miss_over_an_older_good_backup_is_degraded():
    """An older verified backup still stands, but the latest run missed this service. Not an
    emergency (it is still restorable), not silence (it needs attention)."""
    report = evaluate(
        targets=_targets("billing"),
        runs=[_run("r2", "2026-07-25T02:00:00Z", [_r("billing", status="missed", verified=False)]),
              _run("r1", "2026-07-25T01:00:00Z", [_r("billing")])],   # older, verified, recent
        now_iso=NOW, stale_after_seconds=172_800)
    t = _by_service(report)["billing"]
    assert t["status"] == DEGRADED and "latest run missed" in t["reason"]
    assert report["status"] == DEGRADED


def test_a_disabled_target_is_not_alerted_on():
    report = evaluate(targets=[{"service": "paused", "enabled": False}], runs=[],
                      now_iso=NOW, stale_after_seconds=172_800)
    assert report["status"] == HEALTHY and report["alerts"] == []


def test_overall_status_is_the_worst_target():
    report = evaluate(
        targets=_targets("ok", "gone"),
        runs=[_run("r1", "2026-07-25T02:00:00Z", [_r("ok")])],   # 'gone' never appears
        now_iso=NOW, stale_after_seconds=172_800)
    assert report["status"] == CRITICAL
    assert report["summary"]["healthy"] == 1 and report["summary"]["critical"] == 1
