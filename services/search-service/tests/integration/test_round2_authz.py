"""Round-2 fixes (search-service): tenantless principals refused (r2-10), internal DeleteIn validated
identically to IndexIn (r2-12). Maps to docs/audits/round2/search-service.json.
"""
from __future__ import annotations

import datetime as dt
import os

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "h" * 40
ITOKEN = "i" * 40


@pytest.fixture(scope="module")
def client():
    from app.config import get_settings
    saved = dict(os.environ)
    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN=ITOKEN, TOKEN_VERIFIER_DRIVER="hs256",
                      TOKEN_HS256_SECRET=SECRET, RATELIMIT_MAX="1000")
    get_settings.cache_clear()
    from app.main import create_app
    yield TestClient(create_app(), raise_server_exceptions=False)
    os.environ.clear()
    os.environ.update(saved)
    get_settings.cache_clear()


def _tok(tenant="t1", caps=("search.read",)):
    now = dt.datetime.now(dt.timezone.utc)
    claims = {"sub": "u", "caps": list(caps), "exp": now + dt.timedelta(minutes=5), "iat": now}
    if tenant is not None:
        claims["tenant"] = tenant
    t = jwt.encode(claims, SECRET, algorithm="HS256")
    return {"Authorization": "Bearer " + t}


# r2-10 — a principal with no resolved tenant is refused, not collapsed into a shared scope.
def test_tenantless_principal_is_refused(client):
    r = client.post("/api/v1/search", json={"index": "a", "text": "x"}, headers=_tok(tenant=None))
    assert r.status_code == 403
    # a normal tenant is fine
    ok = client.post("/api/v1/search", json={"index": "a", "text": "x"}, headers=_tok(tenant="t1"))
    assert ok.status_code == 200


# r2-12 — internal DeleteIn enforces the same index pattern/length as IndexIn.
def test_internal_delete_validates_index(client):
    bad = client.post("/internal/search/delete",
                      json={"index": "has spaces!", "id": "d1", "tenant": "t1"},
                      headers={"X-Internal-Token": ITOKEN})
    assert bad.status_code == 422
    huge = client.post("/internal/search/delete",
                       json={"index": "a", "id": "x" * 300, "tenant": "t1"},  # > id max_length 256
                       headers={"X-Internal-Token": ITOKEN})
    assert huge.status_code == 422
