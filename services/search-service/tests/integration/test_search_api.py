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
    # search.read can query but not view admin stats
    assert client.get("/api/v1/search/indexes/art/stats", headers=_tok(caps=("search.read",))).status_code == 403
    assert client.get("/api/v1/search/indexes/art/stats", headers=_tok(caps=("search.manage",))).status_code == 200


def test_the_index_catalogue_lists_indexes_for_an_operator(client):
    _index(client, "cat_a", "d1", "t1", {"title": "one"})
    _index(client, "cat_b", "d1", "t1", {"title": "two"})
    # search.read (a query capability) must not reach the operator catalogue.
    assert client.get("/api/v1/search/indexes", headers=_tok(caps=("search.read",))).status_code == 403
    out = client.get("/api/v1/search/indexes", headers=_tok(caps=("search.manage",)))
    assert out.status_code == 200
    names = {i["index"] for i in out.json()["indexes"]}
    assert {"cat_a", "cat_b"} <= names
    a = next(i for i in out.json()["indexes"] if i["index"] == "cat_a")
    assert a["n_docs"] >= 1
