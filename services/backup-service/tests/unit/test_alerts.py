"""Pushing a backup-health alert — best-effort, and never able to take the health check down."""
from __future__ import annotations

from app.core import alerts
from app.core import internal_http


def _report(alerts_list):
    return {"status": "critical", "checked_at": "2026-07-25T12:00:00Z",
            "summary": {"critical": len(alerts_list)}, "alerts": alerts_list}


def test_no_webhook_configured_is_not_a_delivery_but_not_an_error():
    out = alerts.push(_report([{"service": "billing", "severity": "critical"}]),
                      webhook_url="", allowed_hosts=frozenset(), timeout_seconds=5)
    assert out["delivered"] is False and "no webhook" in out["reason"]


def test_nothing_to_alert_on_is_not_pushed():
    out = alerts.push(_report([]), webhook_url="https://alerts.internal/hook",
                      allowed_hosts=frozenset({"alerts.internal"}), timeout_seconds=5)
    assert out["delivered"] is False and out["alerts"] == 0


def test_a_configured_webhook_receives_the_alert(monkeypatch):
    sent = {}

    def fake_post(url, payload, token, *, allowed_hosts, timeout_seconds):
        sent["url"] = url
        sent["payload"] = payload
        return (200, {})

    monkeypatch.setattr(alerts, "post_json", fake_post)
    out = alerts.push(_report([{"service": "billing", "severity": "critical"}]),
                      webhook_url="https://alerts.internal/hook",
                      allowed_hosts=frozenset({"alerts.internal"}), timeout_seconds=5)
    assert out["delivered"] is True
    assert sent["url"] == "https://alerts.internal/hook"
    assert sent["payload"]["source"] == "backup-service"
    assert sent["payload"]["alerts"][0]["service"] == "billing"


def test_an_unreachable_webhook_does_not_raise(monkeypatch):
    def boom(url, payload, token, *, allowed_hosts, timeout_seconds):
        raise internal_http.TargetUnreachable("connection refused")

    monkeypatch.setattr(alerts, "post_json", boom)
    out = alerts.push(_report([{"service": "billing", "severity": "critical"}]),
                      webhook_url="https://alerts.internal/hook",
                      allowed_hosts=frozenset({"alerts.internal"}), timeout_seconds=5)
    assert out["delivered"] is False and "unreachable" in out["reason"]
