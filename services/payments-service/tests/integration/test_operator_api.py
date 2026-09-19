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
SECRET = "sk_live_abcdef0123456789"


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
    """Holds a capability, but not these two. Setting a payment key and reading a report are not
    the same privilege."""
    return _auth("reports.read")


# ── the guarantee ─────────────────────────────────────────────────────────────
def test_no_route_returns_a_stored_credential(client, operator_auth):
    """The whole point. A panel that can read a key back is a panel breach away from every
    tenant's provider account."""
    put = client.put("/api/v1/app/operator/secrets",
                     json={"name": "sms_api_key", "value": SECRET}, headers=operator_auth)
    assert put.status_code == 200, put.text

    bodies = [put.text,
              client.get("/api/v1/app/operator/secrets", headers=operator_auth).text,
              client.get("/api/v1/app/operator/config", headers=operator_auth).text]
    for body in bodies:
        assert SECRET not in body
        # Not even a long prefix: a hint is four characters, and anything more starts being a
        # meaningful fraction of a short token.
        assert SECRET[:8] not in body


def test_the_panel_gets_a_hint_it_can_act_on(client, operator_auth):
    # Enough to tell which key is loaded before replacing it — replacing the wrong one is the
    # mistake the hint prevents.
    client.put("/api/v1/app/operator/secrets",
               json={"name": "sms_api_key", "value": SECRET}, headers=operator_auth)
    got = client.get("/api/v1/app/operator/secrets", headers=operator_auth).json()["secrets"]
    entry = next(s for s in got if s["name"] == "sms_api_key")
    assert entry["hint"].endswith(SECRET[-4:])
    assert entry["hint"].count("•") == 4


# ── authorisation ─────────────────────────────────────────────────────────────
def test_credentials_need_their_own_capability(client, reader_auth):
    """Setting a payment key and reading a report are not the same privilege."""
    for method, url in [("get", "/api/v1/app/operator/secrets"),
                        ("delete", "/api/v1/app/operator/secrets/sms_api_key")]:
        assert getattr(client, method)(url, headers=reader_auth).status_code == 403
    assert client.put("/api/v1/app/operator/secrets",
                      json={"name": "k", "value": "v"}, headers=reader_auth).status_code == 403


def test_unauthenticated_is_refused(client):
    assert client.get("/api/v1/app/operator/secrets").status_code == 401


# ── rotation ──────────────────────────────────────────────────────────────────
def test_a_rotation_keeps_the_previous_value_accepted(client, operator_auth):
    """A request in flight when a key changes must not fail."""
    h = {"headers": operator_auth}
    client.put("/api/v1/app/operator/secrets", json={"name": "k", "value": "old-value-1111"}, **h)
    second = client.put("/api/v1/app/operator/secrets",
                        json={"name": "k", "value": "new-value-2222"}, **h)
    assert second.json()["rotating"] is True, "the previous value was dropped immediately"

    listed = next(s for s in client.get("/api/v1/app/operator/secrets", **h).json()["secrets"]
                  if s["name"] == "k")
    assert listed["rotating"] is True


def test_clearing_a_credential_removes_it(client, operator_auth):
    h = {"headers": operator_auth}
    client.put("/api/v1/app/operator/secrets", json={"name": "k", "value": "v-1234"}, **h)
    assert client.delete("/api/v1/app/operator/secrets/k", **h).json()["cleared"] is True
    assert not [s for s in client.get("/api/v1/app/operator/secrets", **h).json()["secrets"]
                if s["name"] == "k"]


# ── policy ────────────────────────────────────────────────────────────────────
def test_a_deploy_time_knob_is_refused(client, operator_auth):
    """Wiring changed under a live request is a class of bug nobody can reproduce."""
    for key in ("DATASTORE_DRIVER", "SECRETS_KEK", "PII_SALT", "INTERNAL_SERVICE_TOKEN"):
        r = client.put("/api/v1/app/operator/config",
                       json={"key": key, "value": "anything"}, headers=operator_auth)
        assert r.status_code == 400, f"{key} was accepted at runtime"


def test_the_config_view_never_carries_a_credential(client, operator_auth):
    view = client.get("/api/v1/app/operator/config", headers=operator_auth).json()["config"]
    for banned in ("SECRETS_KEK", "INTERNAL_SERVICE_TOKEN", "PII_SALT", "DATASTORE_DSN"):
        assert banned not in view


def test_the_scaffold_declares_no_runtime_policy_so_it_offers_none(client, operator_auth):
    """The template deliberately declares nothing, and the surface must reflect that rather than
    advertising every settable field.

    A generated service adds a key to RUNTIME_POLICY in the same change that wires the read. Until
    then the knob is unavailable, which is honest — an advertised knob nothing consults reports a
    change that never happened, with an audit entry to prove it.
    """
    h = {"headers": operator_auth}
    assert client.get("/api/v1/app/operator/config", **h).json()["config"] == {}
    r = client.put("/api/v1/app/operator/config", json={"key": "RATELIMIT_MAX", "value": 250}, **h)
    assert r.status_code == 400, "an undeclared knob was accepted and would have been inert"


# ── commerce options (enable / lock) ────────────────────────────────────────────
def test_options_list_shows_the_default_before_any_override(client, operator_auth):
    got = client.get("/api/v1/app/operator/options?tenant=acme", headers=operator_auth).json()
    ext = next(o for o in got["options"] if o["option"] == "external_paylink")
    assert ext["enabled"] is True and ext["locked"] is False and ext["source"] == "default"


def test_options_need_the_config_capability(client, reader_auth):
    assert client.get("/api/v1/app/operator/options", headers=reader_auth).status_code == 403
    assert client.put("/api/v1/app/operator/options",
                      json={"option": "external_paylink", "enabled": False},
                      headers=reader_auth).status_code == 403


def test_disabling_the_paylink_option_stops_create_link(client, operator_auth):
    tok = {"X-Internal-Token": "t" * 40}
    hdr = {"headers": tok}
    # Enabled by default: the option gate lets the call through — it then fails on missing merchant
    # creds (409 NOT_CONFIGURED), which still proves the gate passed. What must NEVER appear enabled
    # is the DISABLED refusal.
    p1 = {"tenant": "shopA", "reference": "ord-1", "amount_minor": 1000, "currency": "SAR"}
    r1 = client.post("/internal/payments/requests", json=p1, **hdr)
    assert r1.json().get("code") != "DISABLED"
    # operator disables the option for shopA
    r = client.put("/api/v1/app/operator/options", headers=operator_auth,
                   json={"option": "external_paylink", "enabled": False, "tenant": "shopA"})
    assert r.status_code == 200 and r.json()["enabled"] is False
    # a NEW order for shopA is now refused BY THE GATE, before any gateway work
    p2 = {"tenant": "shopA", "reference": "ord-2", "amount_minor": 1000, "currency": "SAR"}
    r2 = client.post("/internal/payments/requests", json=p2, **hdr)
    assert r2.status_code == 403 and r2.json()["code"] == "DISABLED"
    # another shop is unaffected — its option is still at the default
    p3 = {"tenant": "shopB", "reference": "ord-1", "amount_minor": 1000, "currency": "SAR"}
    assert client.post("/internal/payments/requests", json=p3, **hdr).json().get("code") != "DISABLED"


def test_an_unknown_option_is_refused(client, operator_auth):
    r = client.put("/api/v1/app/operator/options",
                   json={"option": "teleport_pay", "enabled": True}, headers=operator_auth)
    assert r.status_code == 400 and "unknown" in r.json()["reason"].lower()


def test_a_locked_option_refuses_a_change_without_force(client, operator_auth):
    h = {"headers": operator_auth}
    lock = client.put("/api/v1/app/operator/options",
                      json={"option": "external_paylink", "enabled": False, "locked": True,
                            "tenant": "lockshop"}, **h)
    assert lock.status_code == 200 and lock.json()["locked"] is True
    # a plain change is refused …
    blocked = client.put("/api/v1/app/operator/options",
                         json={"option": "external_paylink", "enabled": True, "tenant": "lockshop"}, **h)
    assert blocked.status_code == 400 and "locked" in blocked.json()["reason"].lower()
    # … until force is passed
    forced = client.put("/api/v1/app/operator/options",
                        json={"option": "external_paylink", "enabled": True, "locked": False,
                              "tenant": "lockshop", "force": True}, **h)
    assert forced.status_code == 200 and forced.json()["enabled"] is True


def test_a_platform_lock_overrides_a_tenant_row(client, operator_auth):
    h = {"headers": operator_auth}
    # tenant wants it on …
    client.put("/api/v1/app/operator/options",
               json={"option": "external_paylink", "enabled": True, "tenant": "shopC"}, **h)
    # … but the platform hard-locks it OFF fleet-wide
    client.put("/api/v1/app/operator/options",
               json={"option": "external_paylink", "enabled": False, "locked": True}, **h)
    got = client.get("/api/v1/app/operator/options?tenant=shopC", **h).json()
    ext = next(o for o in got["options"] if o["option"] == "external_paylink")
    assert ext["enabled"] is False and ext["locked"] is True
    # clean up the platform lock so later tests see the default again
    client.delete("/api/v1/app/operator/options/external_paylink?force=true", **h)


# ── availability ──────────────────────────────────────────────────────────────
def test_without_a_kek_the_surface_refuses_rather_than_storing_in_the_clear(env_guard):
    """No KEK means nothing to seal with. Storing the credential anyway — in the clear, while
    every check reports 'encrypted' — is the one outcome that must never happen."""
    c = _make_app(SECRETS_KEK="")
    r = c.put("/api/v1/app/operator/secrets", json={"name": "k", "value": "v"},
              headers=_auth("platform.secrets"))
    assert r.status_code == 503, "a credential was accepted with nothing to seal it with"
    # The reason is not echoed: the error middleware generalises every 5xx, deliberately. The
    # property under test is the refusal and the absence of a stored value, not the wording.
    assert "v" not in r.json().get("error", "")
    # And the read side refuses identically, rather than reporting an empty credential set —
    # "no secrets configured" and "secrets unreadable" must not look the same to an operator.
    assert c.get("/api/v1/app/operator/secrets", headers=_auth("platform.secrets")).status_code == 503
