"""Unit tests for the core Gateway (fake ports; no network)."""
from __future__ import annotations

import asyncio

from starlette.requests import Request

from app.core.gateway import Gateway
from app.core.models import Route
from app.ports.errors import InvalidToken
from app.ports.http_forwarder import UpstreamError


def run(c):
    return asyncio.run(c)


def make_request(method="GET", path="/echo/hi", headers=None, query=b"", body=b"") -> Request:
    scope = {
        "type": "http", "method": method, "path": path, "raw_path": path.encode(),
        "query_string": query, "client": ("1.2.3.4", 5555),
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


class FakeTable:
    driver = "fake"

    def __init__(self, route):
        self._route = route

    def match(self, path):
        return self._route if (self._route and path.startswith(self._route.prefix)) else None

    def all(self):
        return [self._route] if self._route else []


class FakeForwarder:
    driver = "fake"

    def __init__(self, status=200, body=b"OK", raise_error=False):
        self.status, self.body, self.raise_error = status, body, raise_error
        self.last = None

    async def forward(self, method, url, headers, body, params):
        if self.raise_error:
            raise UpstreamError("boom")
        self.last = {"method": method, "url": url, "headers": headers}
        return self.status, {"content-type": "text/plain"}, self.body

    async def close(self):
        pass


class FakeVerifier:
    driver = "fake"

    def __init__(self, claims=None):
        self._claims = claims

    def verify(self, token):
        if self._claims is None:
            raise InvalidToken("bad")
        return self._claims


class FakeLimiter:
    driver = "fake"

    def __init__(self, allow=True):
        self._allow = allow

    async def allow(self, key):
        return self._allow


class FakeCache:
    driver = "fake"

    def __init__(self):
        self.data = {}

    async def get(self, k):
        return self.data.get(k)

    async def set(self, k, v, ttl=None):
        self.data[k] = v


def _gw(route, forwarder=None, verifier=None, limiter=None, cache=None, ttl=0):
    return Gateway(
        route_table=FakeTable(route), forwarder=forwarder or FakeForwarder(),
        token_verifier=verifier or FakeVerifier(), rate_limiter=limiter or FakeLimiter(),
        cache=cache or FakeCache(), internal_token="INTERNAL", default_cache_ttl=ttl,
    )


def test_no_route_404():
    assert run(_gw(None).handle(make_request())).status_code == 404


def test_method_not_allowed_405():
    r = Route(prefix="/echo", upstream="http://up", methods=("GET",))
    assert run(_gw(r).handle(make_request(method="POST"))).status_code == 405


def test_rate_limited_429():
    r = Route(prefix="/echo", upstream="http://up")
    assert run(_gw(r, limiter=FakeLimiter(allow=False)).handle(make_request())).status_code == 429


def test_auth_required_no_token_401():
    r = Route(prefix="/echo", upstream="http://up", auth="required")
    assert run(_gw(r).handle(make_request())).status_code == 401


def test_auth_required_bad_token_401():
    r = Route(prefix="/echo", upstream="http://up", auth="required")
    gw = _gw(r, verifier=FakeVerifier(claims=None))
    resp = run(gw.handle(make_request(headers={"Authorization": "Bearer x"})))
    assert resp.status_code == 401


def test_auth_required_missing_cap_403():
    r = Route(prefix="/echo", upstream="http://up", auth="required", capabilities=("admin",))
    gw = _gw(r, verifier=FakeVerifier(claims={"sub": "u", "caps": ["read"]}))
    resp = run(gw.handle(make_request(headers={"Authorization": "Bearer x"})))
    assert resp.status_code == 403


def test_auth_required_ok_forwards_with_identity():
    r = Route(prefix="/echo", upstream="http://up", auth="required", capabilities=("admin",))
    fwd = FakeForwarder()
    gw = _gw(r, forwarder=fwd, verifier=FakeVerifier(claims={"sub": "u1", "tenant": "t1", "caps": ["admin"]}))
    resp = run(gw.handle(make_request(headers={"Authorization": "Bearer x"})))
    assert resp.status_code == 200
    assert fwd.last["headers"]["X-Internal-Token"] == "INTERNAL"
    assert fwd.last["headers"]["X-User-Id"] == "u1"
    assert fwd.last["headers"]["X-Caps"] == "admin"
    assert fwd.last["url"] == "http://up/hi"  # prefix /echo stripped


def test_upstream_error_504():
    r = Route(prefix="/echo", upstream="http://up")
    gw = _gw(r, forwarder=FakeForwarder(raise_error=True))
    assert run(gw.handle(make_request())).status_code == 504


def test_cache_hit_second_call():
    r = Route(prefix="/echo", upstream="http://up", auth="none", cache_ttl=60)
    fwd = FakeForwarder(body=b"cached")
    cache = FakeCache()
    gw = _gw(r, forwarder=fwd, cache=cache)
    first = run(gw.handle(make_request()))
    assert first.status_code == 200 and "X-Gateway-Cache" not in first.headers
    second = run(gw.handle(make_request()))
    assert second.headers.get("X-Gateway-Cache") == "HIT"
