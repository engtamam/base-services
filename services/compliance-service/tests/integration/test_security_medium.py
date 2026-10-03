"""Security MEDIUM remediation: fail-fast config (compliance-013), operator-DSAR token leak
(compliance-005), guardian-consent write-ahead (compliance-023)."""
from __future__ import annotations

import datetime as dt
import os

import jwt
import pytest
from fastapi.testclient import TestClient

TOKEN_SECRET = "h" * 40
ISS, AUD = "iss", "aud"


def _make_app(**env_over):
    from app.config import get_settings
    env = {"ENV": "test", "INTERNAL_SERVICE_TOKEN": "t" * 40, "TOKEN_VERIFIER_DRIVER": "hs256",
           "TOKEN_HS256_SECRET": TOKEN_SECRET, "TOKEN_ISSUER": ISS, "TOKEN_AUDIENCE": AUD,
           "RATELIMIT_MAX": "1000", "SECRETS_KEK": "unit-test-kek", "PII_SALT": "s" * 32}
    env.update(env_over)
    os.environ.update(env)
    get_settings.cache_clear()
    from app.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


def _auth(*caps):
    now = dt.datetime.now(dt.timezone.utc)
    tok = jwt.encode({"sub": "op1", "iss": ISS, "aud": AUD, "caps": list(caps),
                      "exp": now + dt.timedelta(minutes=5), "iat": now, "tenant": "t1"},
                     TOKEN_SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture(scope="module")
def client():
    saved = dict(os.environ)
    c = _make_app()
    yield c
    os.environ.clear(); os.environ.update(saved)
    from app.config import get_settings
    get_settings.cache_clear()


# ── compliance-005: operator-created DSAR must NOT hand the token back to the operator ──
def test_operator_dsar_does_not_return_the_verification_token(client):
    r = client.post("/api/v1/compliance/dsar",
                    json={"subject": "sub@x.com", "type": "access", "tenant": "t1"},
                    headers=_auth("platform.compliance"))
    assert r.status_code == 201, r.text
    body = r.json()
    assert "verification_token" not in body            # out-of-band only (compliance-005)
    assert body.get("verification_delivery") == "out_of_band"


# ── compliance-013: malformed security config fails closed ──────────────────────
def test_json_map_raises_on_malformed():
    from app.config import _json_any_map
    with pytest.raises(ValueError):
        _json_any_map('{"a":')


def test_json_map_empty_ok():
    from app.config import _json_any_map
    assert _json_any_map("") == {}


# ── compliance-018: ack_target returns the DSAR tenant (so the audit can record it) ──
def test_ack_target_returns_tenant():
    import asyncio

    class _Clock:
        def now(self):
            return 1_700_000_000.0

    class _Dsar:
        def __init__(self):
            self.id = "d1"; self.tenant = "acme"; self.subject = "s"; self.status = "open"
            self.targets = {"svc:cat": False}; self.target_state = {}
        def derive_completion(self, now):
            return False
        def to_dict(self):
            return {}

    class _Store:
        def __init__(self, d):
            self._d = d
        async def get(self, _id):
            return self._d
        async def save(self, _d):
            pass

    from app.core.dsar_service import DsarService
    d = _Dsar()
    svc = DsarService.__new__(DsarService)
    svc._dsars = _Store(d)
    svc._clock = _Clock()
    svc._emit = lambda *a, **k: _noop()
    res = asyncio.run(svc.ack_target("d1", "svc", "cat", ack={"ok": True}))
    assert res["tenant"] == "acme"


async def _noop():
    return None
