"""Timeout & body-limit middleware behave correctly, in isolation."""
from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.middleware.security import BodyLimitMiddleware, TimeoutMiddleware


def _app_with(mw, **kwargs) -> FastAPI:
    app = FastAPI()
    app.add_middleware(mw, **kwargs)

    @app.get("/slow")
    async def slow():
        await asyncio.sleep(0.5)
        return {"ok": True}

    @app.post("/echo")
    async def echo():
        return {"ok": True}

    return app


def test_timeout_returns_408():
    client = TestClient(_app_with(TimeoutMiddleware, timeout_ms=50))
    resp = client.get("/slow")
    assert resp.status_code == 408
    assert resp.json() == {"error": "REQUEST_TIMEOUT"}


def test_body_limit_rejects_oversized():
    client = TestClient(_app_with(BodyLimitMiddleware, max_bytes=16))
    resp = client.post("/echo", content=b"x" * 64)
    assert resp.status_code == 413
    assert resp.json() == {"error": "PAYLOAD_TOO_LARGE"}


def test_body_limit_allows_small():
    client = TestClient(_app_with(BodyLimitMiddleware, max_bytes=1024))
    resp = client.post("/echo", content=b"x" * 8)
    assert resp.status_code == 200
