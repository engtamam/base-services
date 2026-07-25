"""Integration: upload/download/metadata/signed-url/public/delete + validation."""
from __future__ import annotations

import datetime as dt
import os

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "h" * 40
PNG = b"\x89PNG\r\n\x1a\n" + b"data"


def _make_app(**over) -> TestClient:
    from app.config import get_settings

    env = {"ENV": "test", "INTERNAL_SERVICE_TOKEN": "t" * 40,
           "TOKEN_VERIFIER_DRIVER": "hs256", "TOKEN_HS256_SECRET": SECRET,
           "OBJECTSTORE_DRIVER": "memory", "SIGNED_URL_SECRET": "s" * 32, "RATELIMIT_MAX": "1000"}
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


def _h(caps=("files.upload", "files.read"), tenant="t1", sub="alice"):
    now = dt.datetime.now(dt.timezone.utc)
    tok = jwt.encode({"sub": sub, "caps": list(caps), "tenant": tenant,
                      "exp": now + dt.timedelta(minutes=5), "iat": now}, SECRET, algorithm="HS256")
    return {"Authorization": "Bearer " + tok}


def _upload(client, data=PNG, ct="image/png", name="pic.png", **h):
    return client.post("/api/v1/files", content=data,
                       headers={**_h(**h), "Content-Type": ct, "X-Filename": name})


def test_operator_browses_and_inspects_across_tenants(client):
    # A file owned by tenant 'acme'.
    fid = _upload(client, tenant="acme", sub="owner").json()["id"]
    op = {"headers": _h(caps=("platform.files",), tenant="ops", sub="op1")}

    # Browse acme's files as an operator whose own tenant is 'ops'.
    listed = client.get("/api/v1/files/admin/list?tenant=acme", **op)
    assert listed.status_code == 200
    assert any(f["id"] == fid for f in listed.json()["files"])

    # Inspect that file's metadata across the tenant boundary.
    meta = client.get(f"/api/v1/files/admin/{fid}/metadata", **op)
    assert meta.status_code == 200 and meta.json()["id"] == fid


def test_operator_file_routes_need_platform_files(client):
    """A plain files.read/upload user must not reach the cross-tenant operator routes."""
    user = {"headers": _h(caps=("files.read", "files.upload"))}
    assert client.get("/api/v1/files/admin/list?tenant=acme", **user).status_code == 403
    assert client.get("/api/v1/files/admin/x/metadata", **user).status_code == 403
    assert client.get("/api/v1/files/admin/list?tenant=acme").status_code == 401


def test_operator_browse_requires_a_tenant(client):
    op = {"headers": _h(caps=("platform.files",), tenant="ops")}
    assert client.get("/api/v1/files/admin/list", **op).status_code == 422


def test_upload_download_delete(client):
    r = _upload(client)
    assert r.status_code == 201
    fid = r.json()["id"]
    d = client.get(f"/api/v1/files/{fid}", headers=_h())
    assert d.status_code == 200 and d.content == PNG
    m = client.get(f"/api/v1/files/{fid}/metadata", headers=_h())
    assert m.json()["filename"] == "pic.png"
    assert client.delete(f"/api/v1/files/{fid}", headers=_h()).json()["deleted"] is True
    assert client.get(f"/api/v1/files/{fid}", headers=_h()).status_code == 404


def test_signed_url_public_fetch(client):
    fid = _upload(client).json()["id"]
    su = client.post(f"/api/v1/files/{fid}/signed-url", headers=_h()).json()
    pub = client.get(su["url"])  # no auth header
    assert pub.status_code == 200 and pub.content == PNG
    # tampered token -> 403
    assert client.get(su["url"][:-3] + "zzz").status_code == 403


def test_upload_requires_cap(client):
    r = client.post("/api/v1/files", content=PNG,
                    headers={**_h(caps=("files.read",)), "Content-Type": "image/png"})
    assert r.status_code == 403


def _restore(saved):
    from app.config import get_settings

    os.environ.clear()
    os.environ.update(saved)
    get_settings.cache_clear()


def test_oversized_rejected():
    saved = dict(os.environ)
    try:
        small = _make_app(MAX_UPLOAD_BYTES="8", BODY_LIMIT_BYTES="1048576")
        r = small.post("/api/v1/files", content=b"way too many bytes",
                       headers={**_h(), "Content-Type": "application/octet-stream"})
        assert r.status_code == 413
    finally:
        _restore(saved)


def test_blocked_type_rejected():
    saved = dict(os.environ)
    try:
        strict = _make_app(ALLOWED_CONTENT_TYPES="image/png")
        r = strict.post("/api/v1/files", content=b"hello",
                        headers={**_h(), "Content-Type": "text/plain"})
        assert r.status_code == 415
    finally:
        _restore(saved)


def test_cross_tenant_hidden(client):
    fid = _upload(client, tenant="owner-tenant").json()["id"]
    # a different tenant must not see it (404, not 403 — hide existence)
    assert client.get(f"/api/v1/files/{fid}", headers=_h(tenant="other-tenant")).status_code == 404
