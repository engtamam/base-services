"""API regressions for file-service audit findings #01 (chunked size bypass) and #02 (list cap)."""
from __future__ import annotations

import datetime as dt
import os

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "h" * 40


def _make_app(**over) -> TestClient:
    from app.config import get_settings
    env = {"ENV": "test", "INTERNAL_SERVICE_TOKEN": "t" * 40, "TOKEN_VERIFIER_DRIVER": "hs256",
           "TOKEN_HS256_SECRET": SECRET, "OBJECTSTORE_DRIVER": "memory", "SIGNED_URL_SECRET": "s" * 32,
           "RATELIMIT_MAX": "1000"}
    env.update(over)
    os.environ.update(env)
    get_settings.cache_clear()
    from app.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture()
def app_small():
    from app.config import get_settings
    saved = dict(os.environ)
    # inline cap tiny, body limit large — so a chunked body between them tests the ROUTE guard
    c = _make_app(MAX_INLINE_BYTES="8", MAX_UPLOAD_BYTES="8", BODY_LIMIT_BYTES="1048576")
    yield c
    os.environ.clear(); os.environ.update(saved); get_settings.cache_clear()


def _h(caps=("files.upload", "files.read"), tenant="t1", sub="alice"):
    now = dt.datetime.now(dt.timezone.utc)
    tok = jwt.encode({"sub": sub, "caps": list(caps), "tenant": tenant,
                      "exp": now + dt.timedelta(minutes=5), "iat": now}, SECRET, algorithm="HS256")
    return {"Authorization": "Bearer " + tok}


def _chunked(payload: bytes):
    def gen():
        yield payload
    return gen()


def test_chunked_upload_over_inline_cap_is_rejected(app_small):
    # ~100 bytes, sent chunked (no Content-Length), inline cap is 8 → must 413, not buffer+accept
    r = app_small.post("/api/v1/files", content=_chunked(b"x" * 100),
                       headers={**_h(), "Content-Type": "application/octet-stream",
                                "X-Filename": "big.bin"})
    assert r.status_code == 413


def test_list_files_requires_files_read(app_small):
    # a valid token with NO files.read may not enumerate the tenant's files
    r = app_small.get("/api/v1/files", headers=_h(caps=("files.upload",)))
    assert r.status_code == 403
    r2 = app_small.get("/api/v1/files", headers=_h(caps=("files.read",)))
    assert r2.status_code == 200
