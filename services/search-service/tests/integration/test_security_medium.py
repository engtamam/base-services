"""Security MEDIUM remediation: bulk docs get the same field validation as single index (search-005)."""
from __future__ import annotations

import datetime as dt
import os

import jwt
import pytest
from fastapi.testclient import TestClient

ITOKEN = "i" * 40
SECRET = "s" * 40


@pytest.fixture(scope="module")
def client():
    saved = dict(os.environ)
    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN=ITOKEN, TOKEN_VERIFIER_DRIVER="hs256",
                      TOKEN_HS256_SECRET=SECRET, RATELIMIT_MAX="1000")
    from app.config import get_settings
    get_settings.cache_clear()
    from app.main import create_app
    with TestClient(create_app()) as c:
        yield c
    os.environ.clear(); os.environ.update(saved); get_settings.cache_clear()


def _int():
    return {"X-Internal-Token": ITOKEN}


def _bulk(client, docs):
    return client.post("/internal/search/bulk", json={"tenant": "t1", "docs": docs}, headers=_int())


def test_bulk_rejects_empty_id_doc(client):
    r = _bulk(client, [{"index": "art", "id": "", "fields": {"t": "x"}}])
    assert r.status_code == 422


def test_bulk_rejects_bad_index_pattern(client):
    r = _bulk(client, [{"index": "bad index!", "id": "d1", "fields": {"t": "x"}}])
    assert r.status_code == 422


def test_bulk_accepts_a_valid_doc(client):
    r = _bulk(client, [{"index": "art", "id": "d1", "fields": {"t": "x"}}])
    assert r.status_code == 200 and r.json()["indexed"] == 1
