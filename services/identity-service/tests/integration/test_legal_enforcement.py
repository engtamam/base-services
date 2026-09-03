"""Legal-acceptance enforcement at the mint (ToS/Privacy) — the REAL behavioural tests.

These must actually fail if enforcement breaks, so each one drives a login/refresh through the full
app and asserts the concrete outcome: block refuses with a RESTRICTED accept token, acceptance
clears it, flag never blocks, off is inert, refresh is gated too, and the fail modes behave.

The legal authority is a FAKE gate injected into the container (no real compliance-service needed).
Because _svc() reads container.legal_gate and settings.IDENTITY_LEGAL_ENFORCEMENT fresh per request,
swapping the gate and choosing the mode at build time fully exercises the identity-side logic.
"""
from __future__ import annotations

import os

import jwt
import pytest
from fastapi.testclient import TestClient

from app.ports.legal_gate import LegalGateUnavailable

SECRET = "h" * 40


class FakeGate:
    """Controllable stand-in for ComplianceLegalGate. `owed` is what it reports; set `raises` to
    simulate compliance being unreachable. `calls` proves whether it was consulted at all."""
    driver = "fake"

    def __init__(self) -> None:
        self.owed: list[dict] = []
        self.raises: Exception | None = None
        self.calls = 0

    async def outstanding(self, *, subject: str, tenant: str) -> list[dict]:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return list(self.owed)


def _make_app(**over) -> TestClient:
    from app.config import get_settings
    env = {
        "ENV": "test", "INTERNAL_SERVICE_TOKEN": "t" * 40,
        "TOKEN_ISSUER_DRIVER": "hs256", "TOKEN_SIGNING_SECRET": SECRET,
        "TOKEN_VERIFIER_DRIVER": "hs256", "TOKEN_HS256_SECRET": SECRET,
        "RATELIMIT_MAX": "1000", "REGISTRATION_ENABLED": "true",
        # a mode other than off needs an authority configured to pass the boot guard; the real client
        # is then replaced by the fake below, so the URL is never actually dialled.
        "COMPLIANCE_URL": "http://compliance.invalid", "COMPLIANCE_INTERNAL_TOKEN": "c" * 40,
    }
    env.update(over)
    os.environ.update(env)
    get_settings.cache_clear()
    from app.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture
def env_guard():
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)
    from app.config import get_settings
    get_settings.cache_clear()


def _app_with_gate(env_guard, enforcement: str, **over):
    """Build an app in the given enforcement mode and swap in a FakeGate. Returns (client, gate)."""
    client = _make_app(IDENTITY_LEGAL_ENFORCEMENT=enforcement, **over)
    gate = FakeGate()
    client.app.state.container.legal_gate = gate
    return client, gate


def _register(client, username="shopkeeper", password="password1", tenant=""):
    body = {"username": username, "password": password}
    if tenant:
        body["tenant"] = tenant
    r = client.post("/api/v1/auth/register", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _decode(token: str) -> dict:
    return jwt.decode(token, SECRET, algorithms=["HS256"], options={"verify_aud": False})


# ── block mode ────────────────────────────────────────────────────────────────
def test_block_refuses_login_and_hands_back_a_restricted_accept_token(env_guard):
    client, gate = _app_with_gate(env_guard, "block")
    user = _register(client)
    gate.owed = [{"doc_id": "terms", "current_version": "3.0", "reason": "never_accepted"}]

    r = client.post("/api/v1/auth/login", json={"username": "shopkeeper", "password": "password1"})
    assert r.status_code == 403
    body = r.json()
    assert body["error"] == "LEGAL_ACCEPTANCE_REQUIRED"
    assert body["outstanding"] == gate.owed
    # NO usable session was minted — only the restricted accept token.
    assert "access_token" not in body and "refresh_token" not in body
    assert gate.calls == 1

    claims = _decode(body["accept_token"])
    assert claims["caps"] == ["compliance.self"]     # EXACTLY the accept caps, nothing more
    assert "*" not in claims["caps"] and not any(c.startswith("platform.") for c in claims["caps"])
    assert claims.get("legal_only") is True
    assert claims["sub"] == user["id"] and claims["tenant"] == user.get("tenant", "")


def test_block_clears_the_moment_nothing_is_owed(env_guard):
    client, gate = _app_with_gate(env_guard, "block")
    _register(client, username="cleared")
    gate.owed = [{"doc_id": "terms", "current_version": "1.0"}]
    assert client.post("/api/v1/auth/login",
                       json={"username": "cleared", "password": "password1"}).status_code == 403

    gate.owed = []                                   # the shop accepted (compliance now owes nothing)
    r = client.post("/api/v1/auth/login", json={"username": "cleared", "password": "password1"})
    assert r.status_code == 200
    tokens = r.json()
    assert tokens["access_token"] and tokens["refresh_token"]
    assert "legal_pending" not in tokens             # cleared, so nothing is stamped


def test_block_gates_refresh_so_a_new_tos_cannot_be_outlived(env_guard):
    client, gate = _app_with_gate(env_guard, "block")
    _register(client, username="renewer")
    # clean login first
    tokens = client.post("/api/v1/auth/login",
                         json={"username": "renewer", "password": "password1"}).json()
    # a new required doc is published mid-session; the refresh mint must now be refused too
    gate.owed = [{"doc_id": "privacy", "current_version": "2.0", "reason": "version_changed"}]
    r = client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 403
    assert r.json()["error"] == "LEGAL_ACCEPTANCE_REQUIRED"
    assert r.json()["accept_token"]


# ── flag mode ─────────────────────────────────────────────────────────────────
def test_flag_issues_a_normal_token_but_stamps_legal_pending(env_guard):
    client, gate = _app_with_gate(env_guard, "flag")
    _register(client, username="flagged")
    gate.owed = [{"doc_id": "terms", "current_version": "1.0"}]

    r = client.post("/api/v1/auth/login", json={"username": "flagged", "password": "password1"})
    assert r.status_code == 200                       # flag NEVER blocks
    tokens = r.json()
    assert tokens["access_token"] and tokens["refresh_token"]
    assert tokens["legal_pending"] == gate.owed       # surfaced on the response
    assert _decode(tokens["access_token"])["legal_pending"] == gate.owed  # and in the token itself


def test_flag_omits_legal_pending_when_nothing_owed(env_guard):
    client, gate = _app_with_gate(env_guard, "flag")
    _register(client, username="flagclean")
    gate.owed = []
    tokens = client.post("/api/v1/auth/login",
                         json={"username": "flagclean", "password": "password1"}).json()
    assert "legal_pending" not in tokens
    assert "legal_pending" not in _decode(tokens["access_token"])


# ── off mode ──────────────────────────────────────────────────────────────────
def test_off_never_consults_the_gate(env_guard):
    client, gate = _app_with_gate(env_guard, "off")
    gate.raises = RuntimeError("must not be called")  # would blow up if off ever consulted it
    _register(client, username="offuser")
    r = client.post("/api/v1/auth/login", json={"username": "offuser", "password": "password1"})
    assert r.status_code == 200 and gate.calls == 0
    assert "legal_pending" not in r.json()


# ── fail modes (compliance unreachable) ────────────────────────────────────────
def test_fail_open_lets_login_through_when_compliance_is_down(env_guard):
    client, gate = _app_with_gate(env_guard, "block", IDENTITY_LEGAL_GATE_FAIL_MODE="open")
    _register(client, username="failopen")
    gate.raises = LegalGateUnavailable("compliance down")
    r = client.post("/api/v1/auth/login", json={"username": "failopen", "password": "password1"})
    assert r.status_code == 200                       # availability first: a blip must not 503 login
    assert r.json()["access_token"]


def test_fail_closed_refuses_login_when_compliance_is_down(env_guard):
    client, gate = _app_with_gate(env_guard, "block", IDENTITY_LEGAL_GATE_FAIL_MODE="closed")
    _register(client, username="failclosed")
    gate.raises = LegalGateUnavailable("compliance down")
    r = client.post("/api/v1/auth/login", json={"username": "failclosed", "password": "password1"})
    assert r.status_code == 503
    assert r.json()["error"] == "LEGAL_GATE_UNAVAILABLE"


# ── boot guard ─────────────────────────────────────────────────────────────────
def test_enforcement_without_a_configured_authority_refuses_to_boot(env_guard):
    with pytest.raises(ValueError):
        _make_app(IDENTITY_LEGAL_ENFORCEMENT="block", COMPLIANCE_URL="", COMPLIANCE_INTERNAL_TOKEN="")
