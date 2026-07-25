"""Is there a recent, complete, VERIFIED backup of every service? — the only question that matters.

A backup system whose failures are silent is worse than none: it passes the audit and answers
nothing when the restore is needed. So this turns the run history into an answer an operator (and a
monitor) can read, and an alarm that fires on the three ways a backup quietly stops being real:

  MISSED       — a service was unreachable in the latest run (a hint, not yet an emergency if an
                 older verified backup still stands)
  STALE        — no complete+verified backup of a service newer than the operator's tolerance; the
                 cron may have silently stopped, or every recent run failed for that one service
  UNVERIFIED   — the latest backup exists but was never proven restorable (46.4); it does not count
  NEVER        — a registered service has no verified backup at all; it is not protected

The rule 46.4 established, made observable: a backup that was not verified does not count toward
health, does not silence this alarm, and is surfaced as if it were absent — because it may as well be.

Pure and clock-injected: given the registered targets, the run history and 'now', it computes the
report. No I/O here, so it is trivially testable and the alerter and the endpoint share one truth.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

HEALTHY = "healthy"
DEGRADED = "degraded"     # protected, but something needs attention (a recent miss, one stale target)
CRITICAL = "critical"     # a service is unprotected: never verified, or stale past tolerance

_RANK = {HEALTHY: 0, DEGRADED: 1, CRITICAL: 2}


def _epoch(iso: str) -> float | None:
    """Parse an orchestrator timestamp (…Z) to epoch seconds; None if unparseable."""
    if not iso:
        return None
    try:
        return _dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=_dt.timezone.utc).timestamp()
    except (ValueError, TypeError):
        return None


def _target_in(run: dict, service: str) -> dict | None:
    for r in run.get("results", []):
        if r.get("service") == service:
            return r
    return None


def evaluate(*, targets: list[dict], runs: list[dict], now_iso: str,
             stale_after_seconds: int) -> dict[str, Any]:
    """Assess every registered target against the run history.

    `targets` are the registered targets (public dicts with at least `service`, `enabled`); `runs`
    is the run history newest-first (as RunStore.recent returns). Returns a report with an overall
    status and a per-target breakdown, plus the flat alert list an alerter pushes.
    """
    now = _epoch(now_iso) or 0.0
    # Newest first, so the first match walking the list is the most recent.
    ordered = sorted(runs, key=lambda r: r.get("started_at", ""), reverse=True)

    per_target: list[dict[str, Any]] = []
    alerts: list[dict[str, Any]] = []
    for t in targets:
        service = t.get("service")
        if not t.get("enabled", True):
            per_target.append({"service": service, "status": HEALTHY, "reason": "disabled",
                               "enabled": False})
            continue

        last_verified_at = ""
        latest_outcome = "never"        # the target's status in the most recent run that included it
        for run in ordered:
            row = _target_in(run, service)
            if row is None:
                continue
            if latest_outcome == "never":
                latest_outcome = row.get("status", "unknown")
                if row.get("status") != "ok" or not row.get("verified"):
                    latest_outcome = "unverified" if row.get("status") == "ok" else row.get("status")
            if row.get("status") == "ok" and row.get("verified"):
                last_verified_at = run.get("started_at", "")
                break   # the most recent VERIFIED one — its age drives staleness

        verified_epoch = _epoch(last_verified_at)
        age = int(now - verified_epoch) if verified_epoch is not None else None

        if verified_epoch is None:
            status, reason = CRITICAL, ("no verified backup exists — this service is NOT protected"
                                        if latest_outcome != "never"
                                        else "never backed up — this service is NOT protected")
        elif age is not None and age > stale_after_seconds:
            status, reason = CRITICAL, (f"last verified backup is {age}s old, past the "
                                        f"{stale_after_seconds}s tolerance — the schedule may have stopped")
        elif latest_outcome in ("missed", "failed", "unverified"):
            status, reason = DEGRADED, (f"an older verified backup stands, but the latest run "
                                        f"{latest_outcome} this service")
        else:
            status, reason = HEALTHY, "recent verified backup"

        entry = {"service": service, "status": status, "reason": reason,
                 "last_verified_at": last_verified_at, "age_seconds": age,
                 "latest_outcome": latest_outcome, "enabled": True}
        per_target.append(entry)
        if status != HEALTHY:
            alerts.append({"service": service, "severity": status, "reason": reason,
                           "age_seconds": age})

    overall = HEALTHY
    for e in per_target:
        if _RANK[e["status"]] > _RANK[overall]:
            overall = e["status"]
    return {
        "status": overall,
        "checked_at": now_iso,
        "targets": per_target,
        "alerts": alerts,
        "summary": {
            "total": len([t for t in per_target if t.get("enabled", True)]),
            "healthy": sum(1 for e in per_target if e["status"] == HEALTHY and e.get("enabled", True)),
            "degraded": sum(1 for e in per_target if e["status"] == DEGRADED),
            "critical": sum(1 for e in per_target if e["status"] == CRITICAL),
        },
    }
