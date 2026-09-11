"""Integration: index via internal, search via user token, tenant-from-identity, gating."""
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

    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN=ITOKEN, TOKEN_VERIFIER_DRIVER="hs256",
                      TOKEN_HS256_SECRET=SECRET, RATELIMIT_MAX="1000")
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


def _tok(tenant="t1", caps=("search.read",)):
    now = dt.datetime.now(dt.timezone.utc)
    t = jwt.encode({"sub": "u", "caps": list(caps), "tenant": tenant,
                    "exp": now + dt.timedelta(minutes=5), "iat": now}, SECRET, algorithm="HS256")
    return {"Authorization": "Bearer " + t}


def _int():
    return {"X-Internal-Token": ITOKEN}


def _index(client, index, doc_id, tenant, fields, boosts=None):
    return client.post("/internal/search/index",
                       json={"index": index, "id": doc_id, "tenant": tenant, "fields": fields,
                             "boosts": boosts or {}}, headers=_int())


def test_index_and_search(client):
    _index(client, "art", "d1", "t1", {"title": "brown fox"}, {"title": 2})
    _index(client, "art", "d2", "t1", {"title": "lazy dog"})
    r = client.post("/api/v1/search", json={"index": "art", "text": "fox"}, headers=_tok())
    assert r.status_code == 200 and [h["id"] for h in r.json()["hits"]] == ["d1"]


def test_cross_tenant_isolation(client):
    _index(client, "art", "secret", "other-tenant", {"title": "fox secret"})
    hits = client.post("/api/v1/search", json={"index": "art", "text": "fox"}, headers=_tok("t1")).json()["hits"]
    assert all(h["id"] != "secret" for h in hits)


def test_tenant_comes_from_token_not_body(client):
    # a body 'tenant' field is ignored — scope is always the caller's identity
    r = client.post("/api/v1/search", json={"index": "art", "text": "fox", "tenant": "other-tenant"},
                    headers=_tok("t1"))
    assert all(h["id"] != "secret" for h in r.json()["hits"])


def test_wildcard_rejected(client):
    assert client.post("/api/v1/search", json={"index": "art", "text": "fo*"}, headers=_tok()).status_code == 422


def test_arabic_search(client):
    _index(client, "ar", "a1", "t1", {"name": "مُحَمَّد"})
    r = client.post("/api/v1/search", json={"index": "ar", "text": "محمد"}, headers=_tok())
    assert [h["id"] for h in r.json()["hits"]] == ["a1"]


def test_suggest_prefix(client):
    _index(client, "sg", "s1", "t1", {"t": "riyadh"})
    _index(client, "sg", "s2", "t1", {"t": "riyal"})
    out = client.get("/api/v1/search/suggest?index=sg&prefix=riy", headers=_tok()).json()["suggestions"]
    assert "riyadh" in out and "riyal" in out


def test_read_cap_cannot_index_or_see_stats(client):
    # The catalogue/stats are GLOBAL (cross-tenant) in this shared-index engine, so a TENANT operator
    # (search.manage) must NOT read them — only the PLATFORM cap may. search.read (query-only) can't either.
    assert client.get("/api/v1/search/indexes/art/stats", headers=_tok(caps=("search.read",))).status_code == 403
    assert client.get("/api/v1/search/indexes/art/stats", headers=_tok(caps=("search.manage",))).status_code == 403
    assert client.get("/api/v1/search/indexes/art/stats", headers=_tok(caps=("platform.search",))).status_code == 200


def test_the_index_catalogue_is_platform_only_not_tenant_operator(client):
    _index(client, "cat_a", "d1", "t1", {"title": "one"})
    _index(client, "cat_b", "d1", "t1", {"title": "two"})
    # Neither a query cap nor a tenant operator may see the global catalogue (cross-tenant leak).
    assert client.get("/api/v1/search/indexes", headers=_tok(caps=("search.read",))).status_code == 403
    assert client.get("/api/v1/search/indexes", headers=_tok(caps=("search.manage",))).status_code == 403
    out = client.get("/api/v1/search/indexes", headers=_tok(caps=("platform.search",)))
    assert out.status_code == 200
    names = {i["index"] for i in out.json()["indexes"]}
    assert {"cat_a", "cat_b"} <= names


# ── internal (token-gated) suggest + query — the peer-service twins ──────────────────────────
# operations holds only TOKEN_PUBLIC_KEY, so it can never MINT the Bearer token the user routes
# require; the console phone-prefix suggest failed 401 from day one until these existed.
def test_internal_suggest_happy_path(client):
    _index(client, "isg", "i1", "t1", {"t": "riyadh"})
    _index(client, "isg", "i2", "t1", {"t": "riyal"})
    out = client.get("/internal/search/suggest?index=isg&prefix=riy&tenant=t1",
                     headers=_int()).json()["suggestions"]
    assert "riyadh" in out and "riyal" in out


def test_internal_suggest_requires_the_internal_token(client):
    assert client.get("/internal/search/suggest?index=isg&prefix=riy&tenant=t1").status_code == 401


def test_internal_query_happy_path(client):
    _index(client, "iq", "q1", "t1", {"t": "quick brown fox"})
    hits = client.post("/internal/search/query",
                       json={"index": "iq", "tenant": "t1", "text": "fox"},
                       headers=_int()).json()["hits"]
    assert any(h["id"] == "q1" for h in hits)


def test_internal_query_requires_the_internal_token(client):
    assert client.post("/internal/search/query",
                       json={"index": "iq", "tenant": "t1", "text": "fox"}).status_code == 401
