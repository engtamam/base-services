"""Manifest advertises the large-upload capability and its new endpoints."""
from __future__ import annotations

from app.config import Settings
from app.manifest import build_manifest


def _routes(m):
    return {(e["method"], e["path"]) for e in m["endpoints"]}


def test_large_uploads_advertised_available_on_s3():
    m = build_manifest(Settings(OBJECTSTORE_DRIVER="s3", INTERNAL_SERVICE_TOKEN="x" * 32,
                                MAX_INLINE_BYTES=1024, MAX_OBJECT_BYTES=5 << 30))
    feat = m["features"]["large_uploads"]
    assert feat["available"] is True
    assert feat["max_inline_bytes"] == 1024
    assert feat["initiate"] == "/api/v1/files/uploads"


def test_large_uploads_unavailable_on_memory_store():
    m = build_manifest(Settings(OBJECTSTORE_DRIVER="memory", INTERNAL_SERVICE_TOKEN="x" * 32))
    assert m["features"]["large_uploads"]["available"] is False


def test_new_endpoints_are_declared():
    m = build_manifest(Settings(INTERNAL_SERVICE_TOKEN="x" * 32))
    routes = _routes(m)
    assert ("POST", "/api/v1/files/uploads") in routes
    assert ("POST", "/api/v1/files/uploads/{id}/complete") in routes
    assert ("GET", "/api/v1/files/uploads/{id}") in routes
    assert ("DELETE", "/api/v1/files/uploads/{id}") in routes
    assert ("POST", "/internal/files/scan") in routes
    assert ("POST", "/internal/files/sweep-uploads") in routes
