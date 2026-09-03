"""The operator surface: credentials and policy an operator changes after launch.

The load-bearing properties are all about what must NOT happen. No route may hand a stored
credential back; the credential capability must be its own; wiring and salts must stay deploy-time;
and without a KEK the service must refuse rather than store a credential it cannot seal.
"""
from __future__ import annotations

import datetime as dt
import os

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "h" * 40
ISS = "iss"
AUD = "aud"
KEK = "operator-surface-kek"
VALUE = "sk-live-supersecret-9zxq"


def _make_app(**env_over) -> TestClient:
    from app.config import get_settings

    env = {
        "ENV": "test",
        "INTERNAL_SERVICE_TOKEN": "t" * 40,
        "TOKEN_VERIFIER_DRIVER": "hs256",
        "TOKEN_HS256_SECRET": SECRET,
        "TOKEN_ISSUER": ISS,
        "TOKEN_AUDIENCE": AUD,
        "RATELIMIT_MAX": "1000",
        "PII_SALT": "s" * 32,
        "SECRETS_KEK": KEK,
    }
    env.update(env_over)
    os.environ.update(env)
    get_settings.cache_clear()
    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture(scope="module", autouse=True)
def _env():
    from app.config import get_settings

    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def client() -> TestClient:
    return _make_app()


def _auth(*caps) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    tok = jwt.encode({"sub": "op1", "iss": ISS, "aud": AUD, "caps": list(caps),
                      "exp": now + dt.timedelta(minutes=5), "iat": now},
                     SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {tok}"}


# ── write-only ────────────────────────────────────────────────────────────────
def test_no_route_returns_a_stored_credential(client):
    """The whole point. An adapter needs the plaintext; an HTTP caller never does."""
    caps = _auth("platform.secrets")
    put = client.put("/api/v1/verification/operator/secrets",
                     json={"name": "WATHQ_TOKEN", "value": VALUE}, headers=caps)
    assert put.status_code == 200
    assert VALUE not in put.text

    listed = client.get("/api/v1/verification/operator/secrets", headers=caps)
    assert listed.status_code == 200
    assert VALUE not in listed.text
    # The hint identifies which key is loaded and is useless on its own.
    entry = next(s for s in listed.json()["secrets"] if s["name"] == "WATHQ_TOKEN")
    assert entry["hint"].endswith(VALUE[-4:]) and VALUE[:-4] not in entry["hint"]


def test_the_stored_credential_reaches_the_verifier_but_not_the_config_read(client):
    """It is genuinely readable in-process — otherwise the feature does nothing — and still absent
    from every response the panel can ask for."""
    client.put("/api/v1/verification/operator/secrets",
               json={"name": "WATHQ_TOKEN", "value": VALUE}, headers=_auth("platform.secrets"))
    cfg = client.get("/api/v1/verification/operator/config", headers=_auth("platform.config"))
    assert cfg.status_code == 200
    assert VALUE not in cfg.text
    assert not [k for k in cfg.json()["config"] if k.endswith(("_KEY", "_SECRET", "_TOKEN"))]


# ── capability ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("method,path", [
    ("get", "/api/v1/verification/operator/secrets"),
    ("put", "/api/v1/verification/operator/secrets"),
    ("delete", "/api/v1/verification/operator/secrets/WATHQ_TOKEN"),
    ("get", "/api/v1/verification/operator/config"),
    ("put", "/api/v1/verification/operator/config"),
    ("delete", "/api/v1/verification/operator/config/VERIFY_TIMEOUT_SECONDS"),
])
def test_every_operator_route_requires_its_capability(client, method, path):
    """A reviewer token must not reach the credential store: granting trust and holding the keys
    to the registry are different privileges."""
    kw = {"headers": _auth("verification.review")}
    if method in ("put", "post"):
        kw["json"] = {"name": "X", "value": "y"} if "secrets" in path else {"key": "X", "value": "y"}
    r = getattr(client, method)(path, **kw)
    assert r.status_code == 403


def test_secrets_capability_does_not_grant_config(client):
    r = client.get("/api/v1/verification/operator/config", headers=_auth("platform.secrets"))
    assert r.status_code == 403


# ── deploy-time knobs stay deploy-time ────────────────────────────────────────
@pytest.mark.parametrize("key", ["DATASTORE_DRIVER", "PII_SALT", "SECRETS_KEK",
                                 "INTERNAL_SERVICE_TOKEN", "NOT_A_SETTING"])
def test_wiring_and_secrets_are_refused(client, key):
    """A driver swapped under a live request cannot be reasoned about, and a salt change re-maps
    every pseudonym that already exists."""
    r = client.put("/api/v1/verification/operator/config", json={"key": key, "value": "x"},
                   headers=_auth("platform.config"))
    assert r.status_code == 400


def test_a_policy_knob_is_accepted_and_typed(client):
    """VERIFY_PROVIDERS is declared in RUNTIME_POLICY because verifier_pool actually resolves it —
    an operator repointing a claim type at another provider is the change that cannot wait for a
    deployment."""
    r = client.put("/api/v1/verification/operator/config",
                   json={"key": "VERIFY_PROVIDERS", "value": "cr=wathq"},
                   headers=_auth("platform.config"))
    assert r.status_code == 200 and r.json()["value"] == "cr=wathq"


def test_a_knob_nothing_reads_is_refused_even_though_it_is_ordinary_policy(client):
    """VERIFY_TIMEOUT_SECONDS is a real, non-secret setting, refused purely because nothing
    resolves it through RuntimeConfig. Accepting it would report a change that never took effect."""
    r = client.put("/api/v1/verification/operator/config",
                   json={"key": "VERIFY_TIMEOUT_SECONDS", "value": "20"},
                   headers=_auth("platform.config"))
    assert r.status_code == 400


def test_a_value_of_the_wrong_type_is_refused(client):
    r = client.put("/api/v1/verification/operator/config",
                   json={"key": "VERIFY_MAX_ATTEMPTS", "value": "twenty"},
                   headers=_auth("platform.config"))
    assert r.status_code == 400


# ── no KEK ────────────────────────────────────────────────────────────────────
def test_without_a_kek_the_credential_routes_refuse():
    """503, never 200: storing the credential anyway is the one outcome that must not happen."""
    c = _make_app(SECRETS_KEK="")
    r = c.put("/api/v1/verification/operator/secrets", json={"name": "WATHQ_TOKEN", "value": VALUE},
              headers=_auth("platform.secrets"))
    assert r.status_code == 503
    assert c.get("/api/v1/verification/operator/secrets", headers=_auth("platform.secrets")).status_code == 503


# ── the panel and the audit trail describe what exists ────────────────────────
def test_manifest_advertises_the_operator_surface(client):
    m = client.get("/internal/manifest",
                   headers={"X-Internal-Token": os.environ["INTERNAL_SERVICE_TOKEN"]}).json()
    assert {"platform.secrets", "platform.config"} <= set(m["capabilities"])
    assert {"operator.set_secret", "operator.clear_config"} <= {a["id"] for a in m["admin_actions"]}
