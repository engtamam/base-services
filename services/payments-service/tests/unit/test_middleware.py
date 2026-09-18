"""Timeout & body-limit middleware behave correctly, in isolation."""
from __future__ import annotations

import asyncio

from fastapi import FastAPI, Request
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


# ── chunked / no-Content-Length body (the bypass a Content-Length check misses) ────────────────
def _app_reading_body(max_bytes: int) -> FastAPI:
    app = FastAPI()
    app.add_middleware(BodyLimitMiddleware, max_bytes=max_bytes)

    @app.post("/echo")
    async def echo(request: Request):
        return {"len": len(await request.body())}

    return app


def _chunked(payload: bytes):
    # An iterator body → httpx sends Transfer-Encoding: chunked with NO Content-Length.
    def gen():
        yield payload
    return gen()


def test_body_limit_rejects_oversized_chunked():
    client = TestClient(_app_reading_body(2000))
    resp = client.post("/echo", content=_chunked(b"x" * 5000))
    assert resp.status_code == 413


def test_body_limit_allows_and_replays_chunked():
    # under the cap: the handler must still be able to READ the body (it was buffered then replayed).
    client = TestClient(_app_reading_body(2000))
    resp = client.post("/echo", content=_chunked(b"hello"))
    assert resp.status_code == 200 and resp.json()["len"] == 5
