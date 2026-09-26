"""Console gateway webhook-secret check: correct secret verifies, wrong one does not, and the compare
is constant-time (L2 — the pattern a real HMAC adapter copies)."""
from __future__ import annotations

import inspect
import json

from app.adapters.payment.console import ConsoleGateway


def _body(**over):
    d = {"status": "succeeded", "provider_ref": "p", "amount_minor": 100, "currency": "SAR"}
    d.update(over)
    return json.dumps(d).encode()


def test_correct_secret_verifies():
    g = ConsoleGateway(webhook_secret="s3cr3t")
    assert g.verify_webhook(headers={}, body=_body(secret="s3cr3t")).verified is True


def test_wrong_or_absent_secret_does_not_verify():
    g = ConsoleGateway(webhook_secret="s3cr3t")
    assert g.verify_webhook(headers={}, body=_body(secret="wrong")).verified is False
    assert g.verify_webhook(headers={}, body=_body()).verified is False


def test_uses_constant_time_compare_not_equality():
    src = inspect.getsource(ConsoleGateway.verify_webhook)
    assert "compare_digest" in src
    assert "== self._secret" not in src


def test_empty_configured_secret_fails_closed():
    # FIN-027: an EMPTY configured secret must NEVER verify any callback — even one that supplies an
    # empty secret — or a misconfigured tenant would accept forged webhooks. `bool(self._secret)` gates
    # the compare, so an empty secret can never match.
    g = ConsoleGateway(webhook_secret="")
    assert g.verify_webhook(headers={}, body=_body(secret="")).verified is False
    assert g.verify_webhook(headers={}, body=_body(secret="anything")).verified is False
    assert g.verify_webhook(headers={"x-console-secret": ""}, body=_body()).verified is False
