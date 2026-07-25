"""Pushing a backup-health alert to wherever the operator watches.

An alert nobody sees is the silent failure this whole phase is about, one level up. So when health
is not green, the report is recorded (audit) AND — if a destination is configured — pushed to it.
The push is best-effort and never raises into the caller: a down alert channel must not take the
health check down with it, because "the thing that tells you backups are broken is itself broken" is
exactly when you most need the check to still complete and record.
"""
from __future__ import annotations

from typing import Any

from app.core.internal_http import TargetUnreachable, post_json


def push(report: dict[str, Any], *, webhook_url: str, allowed_hosts, timeout_seconds: int) -> dict:
    """POST the alert payload to the webhook. Returns a delivery record; never raises."""
    alerts = report.get("alerts", [])
    if not webhook_url or not alerts:
        return {"delivered": False, "reason": "no webhook configured" if not webhook_url
                else "nothing to alert on", "alerts": len(alerts)}
    payload = {
        "source": "backup-service",
        "status": report.get("status"),
        "checked_at": report.get("checked_at"),
        "summary": report.get("summary", {}),
        "alerts": alerts,
    }
    try:
        status, _ = post_json(webhook_url, payload, token="", allowed_hosts=allowed_hosts,
                              timeout_seconds=timeout_seconds)
    except TargetUnreachable as exc:
        return {"delivered": False, "reason": f"webhook unreachable: {exc}", "alerts": len(alerts)}
    return {"delivered": 200 <= status < 300, "status": status, "alerts": len(alerts)}
