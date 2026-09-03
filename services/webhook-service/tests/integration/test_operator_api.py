"""The operator surface.

The load-bearing tests here are negative ones: that no route hands back a credential, that the
capability is required, and that a knob which must stay deploy-time is refused. The happy paths are
easy and were never the risk.
"""
from __future__ import annotations

import datetime as dt
import os

import jwt
import pytest
from fastapi.testclient import TestClient

TOKEN_SECRET = "h" * 40
ISS, AUD = "iss", "aud"

KEK = "unit-test-kek-not-a-real-key"
SECRET = "whsec_live_abcdef0123456789"


def _make_app(**env_over) -> TestClient:
    from app.config import get_settings

    env = {
        "ENV": "test",
        "INTERNAL_SERVICE_TOKEN": "t" * 40,
        "TOKEN_VERIFIER_DRIVER": "hs256",
        "TOKEN_HS256_SECRET": TOKEN_SECRET,
        "TOKEN_ISSUER": ISS,
        "TOKEN_AUDIENCE": AUD,
        "RATELIMIT_MAX": "1000",
        "INBOUND_SECRETS": "stripe=configured-stripe-secret",
        "SECRETS_KEK": KEK,
        "PII_SALT": "s" * 32,
    }
    env.update(env_over)
    os.environ.update(env)
    get_settings.cache_clear()
    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


def _auth(*caps: str) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    tok = jwt.encode({"sub": "op1", "iss": ISS, "aud": AUD, "caps": list(caps),
                      "exp": now + dt.timedelta(minutes=5), "iat": now},
                     TOKEN_SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture(scope="module")
def env_guard():
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)
    from app.config import get_settings

    get_settings.cache_clear()


@pytest.fixture(scope="module")
def client(env_guard) -> TestClient:
    return _make_app()


@pytest.fixture
def operator_auth() -> dict:
    return _auth("platform.secrets", "platform.config")


@pytest.fixture
def reader_auth() -> dict:
    """Holds a capability, but not these two. Deciding which inbound callbacks are genuine and
    reading a delivery log are not the same privilege."""
    return _auth("webhooks.read")


# ── the guarantee ─────────────────────────────────────────────────────────────
def test_no_route_returns_a_stored_credential(client, operator_auth):
    """The whole point. A panel that can read an inbound secret back is a panel breach away from
    forged events that verify perfectly."""
    put = client.put("/api/v1/webhooks/operator/secrets",
                     json={"name": "INBOUND_SECRET:stripe", "value": SECRET},
                     headers=operator_auth)
    assert put.status_code == 200, put.text

    bodies = [put.text,
              client.get("/api/v1/webhooks/operator/secrets", headers=operator_auth).text,
              client.get("/api/v1/webhooks/operator/config", headers=operator_auth).text]
    for body in bodies:
        assert SECRET not in body
        # Not even a long prefix: a hint is four characters, and anything more starts being a
        # meaningful fraction of a short token.
        assert SECRET[:8] not in body


def test_the_panel_gets_a_hint_it_can_act_on(client, operator_auth):
    # Enough to tell which secret is loaded before replacing it — replacing the wrong source's is
    # the mistake the hint prevents.
    client.put("/api/v1/webhooks/operator/secrets",
               json={"name": "INBOUND_SECRET:stripe", "value": SECRET}, headers=operator_auth)
    got = client.get("/api/v1/webhooks/operator/secrets", headers=operator_auth).json()["secrets"]
    entry = next(s for s in got if s["name"] == "INBOUND_SECRET:stripe")
    assert entry["hint"].endswith(SECRET[-4:])
    assert entry["hint"].count("•") == 4


# ── authorisation ─────────────────────────────────────────────────────────────
def test_credentials_need_their_own_capability(client, reader_auth):
    for method, url in [("get", "/api/v1/webhooks/operator/secrets"),
                        ("delete", "/api/v1/webhooks/operator/secrets/INBOUND_SECRET:stripe")]:
        assert getattr(client, method)(url, headers=reader_auth).status_code == 403
    assert client.put("/api/v1/webhooks/operator/secrets",
                      json={"name": "k", "value": "v"}, headers=reader_auth).status_code == 403


def test_policy_needs_its_own_capability(client, reader_auth):
    assert client.get("/api/v1/webhooks/operator/config", headers=reader_auth).status_code == 403
    assert client.put("/api/v1/webhooks/operator/config",
                      json={"key": "RETRY_MAX_ATTEMPTS", "value": 3},
                      headers=reader_auth).status_code == 403


def test_unauthenticated_is_refused(client):
    assert client.get("/api/v1/webhooks/operator/secrets").status_code == 401


# ── rotation ──────────────────────────────────────────────────────────────────
def test_a_rotation_keeps_the_previous_value_accepted(client, operator_auth):
    """The provider is already signing with whatever it held when it sent. A callback in flight
    has no version to negotiate, and rejecting it looks exactly like a forgery."""
    h = {"headers": operator_auth}
    client.put("/api/v1/webhooks/operator/secrets", json={"name": "k", "value": "old-value-1111"}, **h)
    second = client.put("/api/v1/webhooks/operator/secrets",
                        json={"name": "k", "value": "new-value-2222"}, **h)
    assert second.json()["rotating"] is True, "the previous value was dropped immediately"

    listed = next(s for s in client.get("/api/v1/webhooks/operator/secrets", **h).json()["secrets"]
                  if s["name"] == "k")
    assert listed["rotating"] is True


def test_clearing_a_credential_removes_it(client, operator_auth):
    h = {"headers": operator_auth}
    client.put("/api/v1/webhooks/operator/secrets", json={"name": "k", "value": "v-1234"}, **h)
    assert client.delete("/api/v1/webhooks/operator/secrets/k", **h).json()["cleared"] is True
    assert not [s for s in client.get("/api/v1/webhooks/operator/secrets", **h).json()["secrets"]
                if s["name"] == "k"]


# ── policy ────────────────────────────────────────────────────────────────────
def test_a_deploy_time_knob_is_refused(client, operator_auth):
    """Wiring changed under a live request is a class of bug nobody can reproduce."""
    for key in ("DATASTORE_DRIVER", "SECRETS_KEK", "PII_SALT", "INTERNAL_SERVICE_TOKEN",
                "INBOUND_SECRETS", "TOKEN_HS256_SECRET"):
        r = client.put("/api/v1/webhooks/operator/config",
                       json={"key": key, "value": "anything"}, headers=operator_auth)
        assert r.status_code == 400, f"{key} was accepted at runtime"


def test_the_config_view_never_carries_a_credential(client, operator_auth):
    view = client.get("/api/v1/webhooks/operator/config", headers=operator_auth).json()["config"]
    for banned in ("SECRETS_KEK", "INTERNAL_SERVICE_TOKEN", "PII_SALT", "INBOUND_SECRETS",
                   "OBJECTSTORE_SECRET_KEY"):
        assert banned not in view


def test_a_policy_value_can_be_set_and_cleared(client, operator_auth):
    h = {"headers": operator_auth}
    r = client.put("/api/v1/webhooks/operator/config", json={"key": "RETRY_MAX_ATTEMPTS", "value": 9}, **h)
    assert r.status_code == 200 and r.json()["value"] == 9
    assert client.get("/api/v1/webhooks/operator/config", **h).json()["config"]["RETRY_MAX_ATTEMPTS"] == 9
    assert client.delete("/api/v1/webhooks/operator/config/RETRY_MAX_ATTEMPTS", **h).json()["cleared"] is True


# ── availability ──────────────────────────────────────────────────────────────
def test_without_a_kek_the_surface_refuses_rather_than_storing_in_the_clear(env_guard):
    """No KEK means nothing to seal with. Storing the credential anyway — in the clear, while
    every check reports 'encrypted' — is the one outcome that must never happen."""
    c = _make_app(SECRETS_KEK="")
    r = c.put("/api/v1/webhooks/operator/secrets", json={"name": "k", "value": "v"},
              headers=_auth("platform.secrets"))
    assert r.status_code == 503, "a credential was accepted with nothing to seal it with"
    # The reason is not echoed: the error middleware generalises every 5xx, deliberately. The
    # property under test is the refusal and the absence of a stored value, not the wording.
    assert "v" not in r.json().get("error", "")
    # And the read side refuses identically, rather than reporting an empty credential set —
    # "no secrets configured" and "secrets unreadable" must not look the same to an operator.
    assert c.get("/api/v1/webhooks/operator/secrets", headers=_auth("platform.secrets")).status_code == 503
    _make_app()   # restore the module-scoped environment for anything that follows


def test_a_refresh_window_outliving_the_rotation_window_is_refused(env_guard):
    """Past that point the two stop overlapping and a rotation opens a gap where live replicas
    reject callbacks signed with a secret that is still legitimately in flight."""
    with pytest.raises(ValueError, match="INBOUND_SECRET_REFRESH_SECONDS"):
        _make_app(INBOUND_SECRET_REFRESH_SECONDS="600", SECRETS_ROTATION_WINDOW_SECONDS="300")
    os.environ.pop("INBOUND_SECRET_REFRESH_SECONDS", None)
    os.environ.pop("SECRETS_ROTATION_WINDOW_SECONDS", None)
    _make_app()
