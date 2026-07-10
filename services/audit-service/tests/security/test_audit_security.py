"""Security: append-only, tamper detection end-to-end, deny-by-default."""
from __future__ import annotations

import datetime as dt
import os

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "h" * 40
ITOKEN = "i" * 40


def _make_app() -> TestClient:
    from app.config import get_settings

    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN=ITOKEN,
                      TOKEN_VERIFIER_DRIVER="hs256", TOKEN_HS256_SECRET=SECRET, RATELIMIT_MAX="1000")
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


def _platform():
    now = dt.datetime.now(dt.timezone.utc)
    tok = jwt.encode({"sub": "a", "caps": ["platform.audit"],
                      "exp": now + dt.timedelta(minutes=5), "iat": now}, SECRET, algorithm="HS256")
    return {"Authorization": "Bearer " + tok}


def _internal():
    return {"X-Internal-Token": ITOKEN}


def test_deny_by_default(client):
    assert client.get("/api/v1/audit").status_code == 401
    assert client.get("/api/v1/audit/verify").status_code == 401


def test_no_update_or_delete_route(client):
    # append-only: there is no PUT/DELETE on an audit event.
    assert client.put("/api/v1/audit/1", json={}, headers=_platform()).status_code in (404, 405)
    assert client.delete("/api/v1/audit/1", headers=_platform()).status_code in (404, 405)


def test_tamper_detected_end_to_end(client):
    client.post("/internal/audit", json={"service": "s", "action": "one"}, headers=_internal())
    client.post("/internal/audit", json={"service": "s", "action": "two"}, headers=_internal())
    assert client.get("/api/v1/audit/verify", headers=_platform()).json()["ok"] is True
    # tamper: mutate a stored event directly in the datastore
    import asyncio
    store = client.app.state.container.datastore
    rows = asyncio.run(store.list("audit_events", 100))
    victim = rows[0]
    victim["action"] = "HACKED"
    asyncio.run(store.put("audit_events", victim["id"], victim))
    result = client.get("/api/v1/audit/verify", headers=_platform()).json()
    assert result["ok"] is False and result["first_bad_seq"] is not None
