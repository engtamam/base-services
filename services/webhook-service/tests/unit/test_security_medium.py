"""Security MEDIUM remediation: SSRF deny-list on resolved IP (webhook-004) and fail-closed replay
default (webhook-008)."""
from __future__ import annotations

import inspect

import pytest

from app.core import ssrf


# ── webhook-004: the deny-list is evaluated against the resolved IP, not just the hostname ──
def test_denied_resolved_ip_is_blocked_via_hostname():
    # localhost resolves to 127.0.0.1; even with private allowed, a denied RESOLVED ip must be refused
    with pytest.raises(ssrf.SsrfBlocked):
        ssrf.validate("http://localhost:8000/x", require_https=False, allow_private=True,
                      denied_hosts=frozenset({"127.0.0.1"}))


def test_denied_hostname_still_blocked():
    with pytest.raises(ssrf.SsrfBlocked):
        ssrf.validate("https://evil.example/x", denied_hosts=frozenset({"evil.example"}))


# ── webhook-008: replay_event defaults is_operator=False (fail-closed) ─────────────
def test_replay_event_defaults_fail_closed():
    from app.core.webhook_service import WebhookService
    sig = inspect.signature(WebhookService.replay_event)
    assert sig.parameters["is_operator"].default is False
