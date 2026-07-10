"""Security: magic scan, signed-url tamper/expiry, deny-by-default, header safety."""
from __future__ import annotations

import datetime as dt
import os

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "h" * 40


def _make_app(**over) -> TestClient:
    from app.config import get_settings

    env = {"ENV": "test", "INTERNAL_SERVICE_TOKEN": "t" * 40,
           "TOKEN_VERIFIER_DRIVER": "hs256", "TOKEN_HS256_SECRET": SECRET,
           "OBJECTSTORE_DRIVER": "memory", "SIGNED_URL_SECRET": "s" * 32,
           "SCANNER_DRIVER": "magic", "RATELIMIT_MAX": "1000"}
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


def _h(caps=("files.upload", "files.read")):
    now = dt.datetime.now(dt.timezone.utc)
    tok = jwt.encode({"sub": "u", "caps": list(caps), "tenant": "t1",
                      "exp": now + dt.timedelta(minutes=5), "iat": now}, SECRET, algorithm="HS256")
    return {"Authorization": "Bearer " + tok}


def test_deny_by_default(client):
    assert client.get("/api/v1/files/x").status_code == 401
    assert client.post("/api/v1/files", content=b"x").status_code == 401


def test_magic_blocks_disguised_executable(client):
    # an .exe uploaded as image/png is caught by the magic scanner
    r = client.post("/api/v1/files", content=b"MZ\x90\x00 evil",
                    headers={**_h(), "Content-Type": "image/png", "X-Filename": "pic.png"})
    assert r.status_code == 422


def test_magic_blocks_type_mismatch(client):
    r = client.post("/api/v1/files", content=b"totally not a png",
                    headers={**_h(), "Content-Type": "image/png"})
    assert r.status_code == 422


def test_signed_url_public_requires_valid_token(client):
    assert client.get("/files/signed/not-a-real-token").status_code == 403


def test_filename_header_injection_sanitized(client):
    png = b"\x89PNG\r\n\x1a\n" + b"x"
    r = client.post("/api/v1/files", content=png,
                    headers={**_h(), "Content-Type": "image/png",
                             "X-Filename": 'a"b\r\nSet-Cookie: x'})
    assert r.status_code == 201
    fid = r.json()["id"]
    d = client.get(f"/api/v1/files/{fid}", headers=_h())
    cd = d.headers.get("content-disposition", "")
    assert "\n" not in cd and "Set-Cookie" not in cd.split("filename=")[0]
