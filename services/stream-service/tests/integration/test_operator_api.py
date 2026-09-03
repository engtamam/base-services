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
    put = client.put("/api/v1/sources/operator/secrets",
                     json={"name": "sms_api_key", "value": SECRET}, headers=operator_auth)
    assert put.status_code == 200, put.text

    bodies = [put.text,
              client.get("/api/v1/sources/operator/secrets", headers=operator_auth).text,
              client.get("/api/v1/sources/operator/config", headers=operator_auth).text]
    for body in bodies:
        assert SECRET not in body
        # Not even a long prefix: a hint is four characters, and anything more starts being a
        # meaningful fraction of a short token.
        assert SECRET[:8] not in body


def test_the_panel_gets_a_hint_it_can_act_on(client, operator_auth):
    # Enough to tell which key is loaded before replacing it — replacing the wrong one is the
    # mistake the hint prevents.
    client.put("/api/v1/sources/operator/secrets",
               json={"name": "sms_api_key", "value": SECRET}, headers=operator_auth)
    got = client.get("/api/v1/sources/operator/secrets", headers=operator_auth).json()["secrets"]
    entry = next(s for s in got if s["name"] == "sms_api_key")
    assert entry["hint"].endswith(SECRET[-4:])
    assert entry["hint"].count("•") == 4


# ── authorisation ─────────────────────────────────────────────────────────────
def test_credentials_need_their_own_capability(client, reader_auth):
    """Setting a payment key and reading a report are not the same privilege."""
    for method, url in [("get", "/api/v1/sources/operator/secrets"),
                        ("delete", "/api/v1/sources/operator/secrets/sms_api_key")]:
        assert getattr(client, method)(url, headers=reader_auth).status_code == 403
    assert client.put("/api/v1/sources/operator/secrets",
                      json={"name": "k", "value": "v"}, headers=reader_auth).status_code == 403


def test_unauthenticated_is_refused(client):
    assert client.get("/api/v1/sources/operator/secrets").status_code == 401


# ── rotation ──────────────────────────────────────────────────────────────────
def test_a_rotation_keeps_the_previous_value_accepted(client, operator_auth):
    """A request in flight when a key changes must not fail."""
    h = {"headers": operator_auth}
    client.put("/api/v1/sources/operator/secrets", json={"name": "k", "value": "old-value-1111"}, **h)
    second = client.put("/api/v1/sources/operator/secrets",
                        json={"name": "k", "value": "new-value-2222"}, **h)
    assert second.json()["rotating"] is True, "the previous value was dropped immediately"

    listed = next(s for s in client.get("/api/v1/sources/operator/secrets", **h).json()["secrets"]
                  if s["name"] == "k")
    assert listed["rotating"] is True


def test_clearing_a_credential_removes_it(client, operator_auth):
    h = {"headers": operator_auth}
    client.put("/api/v1/sources/operator/secrets", json={"name": "k", "value": "v-1234"}, **h)
    assert client.delete("/api/v1/sources/operator/secrets/k", **h).json()["cleared"] is True
    assert not [s for s in client.get("/api/v1/sources/operator/secrets", **h).json()["secrets"]
                if s["name"] == "k"]


# ── policy ────────────────────────────────────────────────────────────────────
def test_a_deploy_time_knob_is_refused(client, operator_auth):
    """Wiring changed under a live request is a class of bug nobody can reproduce."""
    for key in ("DATASTORE_DRIVER", "SECRETS_KEK", "PII_SALT", "INTERNAL_SERVICE_TOKEN"):
        r = client.put("/api/v1/sources/operator/config",
                       json={"key": key, "value": "anything"}, headers=operator_auth)
        assert r.status_code == 400, f"{key} was accepted at runtime"


def test_the_config_view_never_carries_a_credential(client, operator_auth):
    view = client.get("/api/v1/sources/operator/config", headers=operator_auth).json()["config"]
    for banned in ("SECRETS_KEK", "INTERNAL_SERVICE_TOKEN", "PII_SALT", "DATASTORE_DSN"):
        assert banned not in view


def test_this_service_declares_no_runtime_policy_so_it_offers_none(client, operator_auth):
    """It manages credentials at runtime, not policy — and the surface must say so.

    Advertising a knob nothing reads is worse than not offering it: the panel reports success and
    the audit log records a change that never took effect, leaving an operator holding a receipt
    for a limit that was never raised. A key is added to RUNTIME_POLICY in the same change that
    wires the read.
    """
    h = {"headers": operator_auth}
    assert client.get("/api/v1/sources/operator/config", **h).json()["config"] == {}
    r = client.put("/api/v1/sources/operator/config", json={"key": "KNOB", "value": 9}, **h)
    assert r.status_code == 400, "an undeclared knob was accepted and would have been inert"

# ── availability ──────────────────────────────────────────────────────────────
def test_without_a_kek_the_surface_refuses_rather_than_storing_in_the_clear(env_guard):
    """No KEK means nothing to seal with. Storing the credential anyway — in the clear, while
    every check reports 'encrypted' — is the one outcome that must never happen."""
    c = _make_app(SECRETS_KEK="")
    r = c.put("/api/v1/sources/operator/secrets", json={"name": "k", "value": "v"},
              headers=_auth("platform.secrets"))
    assert r.status_code == 503, "a credential was accepted with nothing to seal it with"
    # The reason is not echoed: the error middleware generalises every 5xx, deliberately. The
    # property under test is the refusal and the absence of a stored value, not the wording.
    assert "v" not in r.json().get("error", "")
    # And the read side refuses identically, rather than reporting an empty credential set —
    # "no secrets configured" and "secrets unreadable" must not look the same to an operator.
    assert c.get("/api/v1/sources/operator/secrets", headers=_auth("platform.secrets")).status_code == 503


# ── what this service actually consumes ───────────────────────────────────────
def test_the_recording_sink_uses_the_stored_token_and_falls_back_to_config():
    """The point of the whole migration: the sink authenticates with what the operator set, and a
    deployment that set nothing behaves exactly as it did before."""
    import asyncio

    from app.adapters.stream.file_service_sink import FileServiceRecordingSink
    from app.core.operator_credentials import CredentialResolver
    from app.core.secret_store import SecretStore
    from app.adapters.datastore.memory import MemoryDataStore

    seen: list[dict] = []

    async def transport(method, url, headers, content):
        seen.append(headers)
        return 201, {"id": "f1"}

    async def scenario():
        ds = MemoryDataStore()
        store = SecretStore(ds, KEK)
        resolver = CredentialResolver(store, "RECORDING_FILE_SERVICE_TOKEN", "from-env", ttl_s=0)
        sink = FileServiceRecordingSink("http://files", "from-env", transport=transport,
                                        token_resolver=resolver)
        await sink.put_segment(tenant="t", session_id="s", seq=1, data=b"x",
                               content_type="video/mp4", start="a", end="b")
        await store.set(tenant="", name="RECORDING_FILE_SERVICE_TOKEN", value="set-by-operator")
        resolver.invalidate()
        await sink.put_segment(tenant="t", session_id="s", seq=2, data=b"x",
                               content_type="video/mp4", start="a", end="b")

    asyncio.run(scenario())
    assert seen[0]["Authorization"] == "Bearer from-env", "nothing stored must behave as today"
    assert seen[1]["Authorization"] == "Bearer set-by-operator", "the stored token was ignored"


def test_a_cached_credential_is_dropped_when_the_panel_writes(env_guard, operator_auth):
    """An operator who sets a token and immediately records a test clip must not wait out the
    refresh window on the replica they are talking to."""
    client = _make_app(RECORDING_SINK_DRIVER="file-service",
                       RECORDING_FILE_SERVICE_URL="http://files")
    resolvers = client.app.state.container.credential_resolvers
    assert resolvers, "no credential is resolved at runtime — the migration is decorative"
    for r in resolvers:
        r._cached, r._until = "stale", float("inf")
    client.put("/api/v1/sources/operator/secrets",
               json={"name": "RECORDING_FILE_SERVICE_TOKEN", "value": "tok-9999"},
               headers=operator_auth)
    assert all(r._cached is None for r in resolvers), "this replica kept the old credential"


def test_this_services_own_credentials_are_not_settable_as_config(client, operator_auth):
    """SEAL_KEY protects every stored camera password; a token uploads footage. Neither may be set
    through the config route, where it would be stored in the clear and read back."""
    for key in ("SEAL_KEY", "RECORDING_FILE_SERVICE_TOKEN", "PLAYBACK_SIGNING_SECRET",
                "TURN_SECRET"):
        r = client.put("/api/v1/sources/operator/config", json={"key": key, "value": "x"},
                       headers=operator_auth)
        assert r.status_code == 400, f"{key} was accepted at runtime"
    view = client.get("/api/v1/sources/operator/config", headers=operator_auth).json()["config"]
    assert "SEAL_KEY" not in view and "RECORDING_FILE_SERVICE_TOKEN" not in view
