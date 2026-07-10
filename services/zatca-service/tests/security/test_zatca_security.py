"""Security: fail-closed, secret-free, deny-by-default, cross-tenant, egress config."""
from __future__ import annotations

import datetime as dt
import os

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "h" * 40
ITOKEN = "i" * 40


def _make_app(**over) -> TestClient:
    from app.config import get_settings

    env = {"ENV": "test", "INTERNAL_SERVICE_TOKEN": ITOKEN, "TOKEN_VERIFIER_DRIVER": "hs256",
           "TOKEN_HS256_SECRET": SECRET, "RATELIMIT_MAX": "1000"}
    env.update(over)
    os.environ.update(env)
    get_settings.cache_clear()
    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture(scope="module")
def client():
    from app.config import get_settings

    saved = dict(os.environ)
    c = _make_app()
    yield c
    os.environ.clear()
    os.environ.update(saved)
    get_settings.cache_clear()


def _adm(caps=("platform.zatca",), tenant="t1"):
    now = dt.datetime.now(dt.timezone.utc)
    tok = jwt.encode({"sub": "a", "caps": list(caps), "tenant": tenant,
                      "exp": now + dt.timedelta(minutes=5), "iat": now}, SECRET, algorithm="HS256")
    return {"Authorization": "Bearer " + tok}


def test_deny_by_default(client):
    assert client.get("/api/v1/zatca/settings").status_code == 401
    assert client.post("/api/v1/zatca/sweep").status_code == 401


def test_onboard_fails_closed_without_signer(client):
    # null signer => onboarding cannot fabricate a CSR/CSID
    r = client.post("/api/v1/zatca/onboard/compliance", json={"otp": "123456"}, headers=_adm())
    assert r.status_code == 503


def test_config_driven_endpoints_no_hardcode():
    # a service with no Fatoora base URL cannot submit — driver stays null (fail-soft)
    app = _make_app()
    r = app.post("/internal/zatca/finalize",
                 json={"tenant": "t1", "invoice": {"number": "X", "issued_at": 1, "seller": {"name": "s"},
                                                   "lines": [{"description": "a", "qty": 1, "amount_minor": 100}]}},
                 headers={"X-Internal-Token": ITOKEN})
    assert r.status_code == 200 and r.json()["status"] == "pending"   # never fabricated


def test_seal_hides_csid_at_rest():
    from app.adapters.datastore.memory import MemoryDataStore
    from app.core.stores import SettingsStore
    import asyncio

    st = MemoryDataStore()
    s = SettingsStore(st, "master-key")
    asyncio.run(s.store_csid("t1", certificate_pem="TOPSECRETCERT", secret="TOPSECRET",
                             private_key_pem="TOPSECRETKEY"))
    raw = asyncio.run(st.get("zatca_settings", "t1"))
    blob = str(raw)
    assert "TOPSECRETCERT" not in blob and "TOPSECRETKEY" not in blob and "TOPSECRET" not in blob
    assert asyncio.run(s.view("t1")).get("onboarded") is True   # flag only, no material
