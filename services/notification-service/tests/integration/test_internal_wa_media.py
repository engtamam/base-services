"""POST /internal/whatsapp/media/upload-handle — the resumable-upload seam for carousel media.

The platform owns upload→handle so a caller never runs Meta's two-step upload by hand. Token-authed,
JSON+base64 body, validates the MIME type and size, returns the handle (console driver in dev).
"""
from __future__ import annotations

import base64

import pytest
from starlette.testclient import TestClient

from app.config import get_settings
from app.main import create_app


@pytest.fixture()
def client():
    get_settings.cache_clear()
    yield TestClient(create_app())
    get_settings.cache_clear()


def _tok():
    return {"X-Internal-Token": get_settings().INTERNAL_SERVICE_TOKEN}


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_upload_requires_the_internal_token():
    c = TestClient(create_app())
    assert c.post("/internal/whatsapp/media/upload-handle",
                  json={"tenant": "acme", "file_type": "image/png",
                        "data_base64": _b64(b"x")}).status_code == 401


def test_upload_returns_a_handle(client):
    r = client.post("/internal/whatsapp/media/upload-handle", headers=_tok(),
                    json={"tenant": "acme", "file_type": "image/png", "file_name": "hero.png",
                          "data_base64": _b64(b"\x89PNG image data")})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] and r.json()["handle"].startswith("console-handle:")


def test_upload_rejects_an_unsupported_type(client):
    r = client.post("/internal/whatsapp/media/upload-handle", headers=_tok(),
                    json={"tenant": "acme", "file_type": "application/zip",
                          "data_base64": _b64(b"x")})
    assert r.status_code == 422


def test_upload_rejects_malformed_base64(client):
    r = client.post("/internal/whatsapp/media/upload-handle", headers=_tok(),
                    json={"tenant": "acme", "file_type": "image/png", "data_base64": "not!base64!"})
    assert r.status_code == 422


def test_upload_rejects_over_the_size_ceiling(client):
    big = _b64(b"x" * (get_settings().WHATSAPP_MEDIA_MAX_BYTES + 1))
    r = client.post("/internal/whatsapp/media/upload-handle", headers=_tok(),
                    json={"tenant": "acme", "file_type": "image/png", "data_base64": big})
    assert r.status_code == 413


def test_upload_id_returns_a_media_id(client):
    r = client.post("/internal/whatsapp/media/upload-id", headers=_tok(),
                    json={"tenant": "acme", "file_type": "image/png",
                          "data_base64": _b64(b"\x89PNG image data")})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] and r.json()["media_id"].startswith("console-media:")


def test_upload_id_requires_the_internal_token():
    c = TestClient(create_app())
    assert c.post("/internal/whatsapp/media/upload-id",
                  json={"tenant": "acme", "file_type": "image/png",
                        "data_base64": _b64(b"x")}).status_code == 401
