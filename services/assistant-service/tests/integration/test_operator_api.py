"""The operator surface, end to end.

The guarantee these defend is negative and therefore easy to lose: NO route returns a stored
credential. A test that only checked "set then list works" would pass against a build that handed
the value back in the listing, which is the one outcome the whole design exists to prevent.

The rest pin the boundaries an operator can walk into: the capability is separate from every other
one this service grants, deploy-time wiring is refused rather than stored and ignored, and a
service with no KEK refuses instead of writing a credential in the clear.
"""
from __future__ import annotations

import datetime as dt
import os

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "h" * 40
ISS, AUD = "iss", "aud"
KEK = "kek-for-the-tests-only-not-a-real-one"
STORED = "sk-stored-by-the-operator-9f2c"


def _tok(caps, sub="op-1", tenant="t1"):
    now = dt.datetime.now(dt.timezone.utc)
    claims = {"sub": sub, "iss": ISS, "aud": AUD, "tenant": tenant, "caps": list(caps),
              "iat": now, "exp": now + dt.timedelta(minutes=10)}
    return {"Authorization": "Bearer " + jwt.encode(claims, SECRET, algorithm="HS256")}


def _env(**extra):
    return {"ENV": "test", "INTERNAL_SERVICE_TOKEN": "t" * 40,
            "TOKEN_VERIFIER_DRIVER": "hs256", "TOKEN_HS256_SECRET": SECRET,
            "TOKEN_ISSUER": ISS, "TOKEN_AUDIENCE": AUD, "RATELIMIT_MAX": "1000",
            "PII_SALT": "s" * 32, "MODEL_API_KEY": "from-the-environment", **extra}


def _client(**extra):
    from app.config import get_settings

    saved = dict(os.environ)
    os.environ.update(_env(**extra))
    get_settings.cache_clear()
    from app.main import create_app
    app = create_app()
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        os.environ.clear(); os.environ.update(saved); get_settings.cache_clear()


@pytest.fixture()
def client():
    yield from _client(SECRETS_KEK=KEK)


@pytest.fixture()
def keyless_client():
    """A deployment that never set a KEK. It must refuse, not store in the clear."""
    yield from _client()


CAPS = ["platform.secrets", "platform.config"]


# ── the negative guarantee ────────────────────────────────────────────────────
def test_no_route_returns_the_stored_credential(client):
    assert client.put("/api/v1/operator/secrets", headers=_tok(CAPS),
                      json={"name": "MODEL_API_KEY", "value": STORED}).status_code == 200

    for resp in (client.put("/api/v1/operator/secrets", headers=_tok(CAPS),
                            json={"name": "MODEL_API_KEY", "value": STORED}),
                 client.get("/api/v1/operator/secrets", headers=_tok(CAPS)),
                 client.get("/api/v1/operator/config", headers=_tok(CAPS))):
        assert resp.status_code == 200
        assert STORED not in resp.text, f"{resp.request.url} handed back the credential"


def test_the_listing_shows_a_hint_only(client):
    client.put("/api/v1/operator/secrets", headers=_tok(CAPS),
               json={"name": "MODEL_API_KEY", "value": STORED})
    body = client.get("/api/v1/operator/secrets", headers=_tok(CAPS)).json()
    row = next(s for s in body["secrets"] if s["name"] == "MODEL_API_KEY")
    # Four characters identify WHICH key is loaded — enough to avoid replacing the wrong one, and
    # useless to anyone who steals the response.
    assert row["hint"].endswith(STORED[-4:]) and STORED[:-4] not in row["hint"]


# ── authorisation ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("path,method", [
    ("/api/v1/operator/secrets", "get"),
    ("/api/v1/operator/secrets", "put"),
    ("/api/v1/operator/secrets/MODEL_API_KEY", "delete"),
    ("/api/v1/operator/config", "get"),
    ("/api/v1/operator/config", "put"),
    ("/api/v1/operator/config/MODEL_NAME", "delete"),
])
def test_the_capability_is_required(client, path, method):
    """Holding every OTHER capability this service grants must not open the operator surface."""
    call = getattr(client, method)
    kwargs = {"json": {"name": "X", "value": "y", "key": "MODEL_NAME"}} if method == "put" else {}
    assert call(path, headers=_tok(["assistant.ask", "platform.assistant"]),
                **kwargs).status_code == 403
    assert call(path, **kwargs).status_code == 401


# ── what may not be changed at runtime ────────────────────────────────────────
@pytest.mark.parametrize("key", ["DATASTORE_DRIVER", "PII_SALT", "SECRETS_KEK",
                                 "MODEL_API_KEY", "NO_SUCH_KNOB"])
def test_deploy_time_knobs_are_refused(client, key):
    """Wiring, identity and credentials are deploy-time. Storing one here would be worse than
    refusing: it would be accepted, never read, and believed."""
    r = client.put("/api/v1/operator/config", headers=_tok(CAPS),
                   json={"key": key, "value": "memory"})
    assert r.status_code == 400
    cfg = client.get("/api/v1/operator/config", headers=_tok(CAPS)).json()["config"]
    assert key not in cfg


def test_this_service_declares_no_runtime_policy_so_it_offers_none(client):
    """It manages credentials at runtime, not policy — and the surface must say so.

    Advertising a knob nothing reads is worse than not offering it: the panel reports success and
    the audit log records a change that never took effect, leaving an operator holding a receipt
    for a limit that was never raised. A key is added to RUNTIME_POLICY in the same change that
    wires the read.
    """
    h = {"headers": _tok(CAPS)}
    assert client.get("/api/v1/operator/config", **h).json()["config"] == {}
    r = client.put("/api/v1/operator/config", json={"key": "KNOB", "value": 9}, **h)
    assert r.status_code == 400, "an undeclared knob was accepted and would have been inert"


# ── no KEK ────────────────────────────────────────────────────────────────────
def test_without_a_kek_the_surface_refuses(keyless_client):
    """503, not a successful write. There is nothing to seal with, and storing it anyway is the
    one outcome that must never happen."""
    r = keyless_client.put("/api/v1/operator/secrets", headers=_tok(CAPS),
                           json={"name": "MODEL_API_KEY", "value": STORED})
    assert r.status_code == 503
    assert keyless_client.get("/api/v1/operator/secrets",
                              headers=_tok(CAPS)).status_code == 503


# ── the credential actually reaches the model ─────────────────────────────────
def test_a_stored_key_beats_the_environment_one_and_rotates(client):
    """The point of the phase: a key set through the panel is what the model call authenticates
    with, and a rotation lands without a redeploy."""
    import asyncio

    creds = client.app.state.model_credentials
    assert asyncio.run(creds.get()) == "from-the-environment"

    client.put("/api/v1/operator/secrets", headers=_tok(CAPS),
               json={"name": "MODEL_API_KEY", "value": STORED})
    assert asyncio.run(creds.get()) == STORED, "the write did not invalidate this replica's cache"

    client.put("/api/v1/operator/secrets", headers=_tok(CAPS),
               json={"name": "MODEL_API_KEY", "value": "sk-rotated-3a1b"})
    assert asyncio.run(creds.get()) == "sk-rotated-3a1b"

    client.delete("/api/v1/operator/secrets/MODEL_API_KEY", headers=_tok(CAPS))
    assert asyncio.run(creds.get()) == "from-the-environment"


def test_every_mutation_is_audited(client):
    """A credential change that cannot be recorded must not happen, so it is recorded first."""
    seen = []
    auditor = client.app.state.auditor
    real = auditor.record

    async def spy(action, **kw):
        seen.append((action, kw.get("resource", "")))
        return await real(action, **kw)

    auditor.record = spy
    try:
        client.put("/api/v1/operator/secrets", headers=_tok(CAPS),
                   json={"name": "MODEL_API_KEY", "value": STORED})
        client.delete("/api/v1/operator/secrets/MODEL_API_KEY", headers=_tok(CAPS))
        client.put("/api/v1/operator/config", headers=_tok(CAPS),
                   json={"key": "MODEL_MAX_ANSWER_TOKENS", "value": "256"})
    finally:
        auditor.record = real

    assert [a for a, _ in seen] == ["operator.secret_set", "operator.secret_cleared",
                                    "operator.config_changed"]
    # The NAME, never the value: an audit row is durable and widely readable.
    assert all(STORED not in res for _, res in seen)
