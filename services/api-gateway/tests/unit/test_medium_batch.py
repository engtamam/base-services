"""Non-security MEDIUM remediation: capmatch wildcard boundary (014), rate-limit prefix boundary (006),
502-vs-504 (012), corrupt-cache miss (015), fail-closed rate-limit (020)."""
from __future__ import annotations

import asyncio

from app.core.capmatch import holds
from app.core.gateway import Gateway
from app.core.models import Route
from app.core.ratelimit_policy import RateLimitPolicy
from app.ports.http_forwarder import UpstreamError


def run(c):
    return asyncio.run(c)


# ── api-gateway-014: wildcard grants require a namespace boundary ─────────────────
def test_wildcard_requires_dot_boundary():
    assert holds({"*"}, "platform.secrets") is True
    assert holds({"platform.*"}, "platform.secrets") is True
    assert holds({"plat*"}, "platform.secrets") is False          # no over-grant across the boundary
    assert holds({"files.*"}, "files.read") is True
    assert holds({"files.*"}, "filesystem.read") is False


# ── api-gateway-006: rate-limit classify matches on a segment boundary ────────────
def test_classify_segment_boundary():
    pol = RateLimitPolicy.from_config(
        key_parts="ip",
        routes_json='[{"prefix":"/api/v1/auth","class":"auth","limit":10,"window_s":60,"cost":1}]',
        default_limit=100, default_window_s=60)
    assert pol.classify("/api/v1/auth").name == "auth"
    assert pol.classify("/api/v1/auth/login").name == "auth"
    assert pol.classify("/api/v1/authz").name == "default"        # not swallowed by /api/v1/auth


# ── a tiny gateway for the forward-path cases ─────────────────────────────────────
class _F:
    driver = "fake"


class _Route:
    def __init__(self):
        self.prefix = "/echo"
        self.auth = "none"
        self.cache_ttl = 0

    def matches_path(self, p):
        return p.startswith("/echo")

    def allows_method(self, m):
        return True

    def build_url(self, p):
        return "http://upstream/echo"


class _Table:
    driver = "fake"

    def match(self, path):
        return _Route() if path.startswith("/echo") else None

    def all(self):
        return [_Route()]


class _Forwarder:
    driver = "fake"

    def __init__(self, exc=None):
        self._exc = exc

    async def forward(self, *a, **k):
        if self._exc:
            raise self._exc
        return (200, {"content-type": "text/plain"}, b"ok")

    async def close(self):
        pass


class _Limiter:
    driver = "fake"

    def __init__(self, boom=False):
        self._boom = boom

    async def allow(self, *a, **k):
        if self._boom:
            raise RuntimeError("limiter down")
        return True

    async def check(self, *a, **k):
        if self._boom:
            raise RuntimeError("limiter down")
        from app.ports.rate_limiter import Verdict
        return Verdict(allowed=True, limit=100, remaining=99, retry_after_s=0, reset_s=60)


class _Cache:
    driver = "fake"

    def __init__(self, value=None):
        self.value = value
        self.deleted = []

    async def get(self, k):
        return self.value

    async def set(self, k, v, ttl=None):
        pass

    async def delete(self, k):
        self.deleted.append(k)
        return True


def _make_request(method="GET", path="/echo/x"):
    from starlette.requests import Request
    scope = {"type": "http", "method": method, "path": path, "raw_path": path.encode(),
             "query_string": b"", "client": ("1.2.3.4", 5555), "headers": []}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)


def _gw(forwarder=None, cache=None, limiter=None):
    return Gateway(route_table=_Table(), forwarder=forwarder or _Forwarder(),
                   token_verifier=_F(), rate_limiter=limiter or _Limiter(),
                   cache=cache or _Cache(), internal_token="INTERNAL", default_cache_ttl=30)


# ── api-gateway-012: timeout → 504, generic upstream failure → 502 ────────────────
def test_upstream_timeout_is_504():
    r = run(_gw(forwarder=_Forwarder(UpstreamError("slow", timeout=True))).handle(_make_request()))
    assert r.status_code == 504


def test_upstream_generic_is_502():
    r = run(_gw(forwarder=_Forwarder(UpstreamError("refused"))).handle(_make_request()))
    assert r.status_code == 502


# ── api-gateway-015: a corrupt cache entry is a miss, not a 500 ───────────────────
def test_corrupt_cache_entry_is_a_miss():
    cache = _Cache(value="::not-valid-cached::")
    r = run(_gw(cache=cache).handle(_make_request()))
    assert r.status_code == 200 and r.body == b"ok"      # fell through to the upstream
    assert cache.deleted                                 # and evicted the bad key


# ── api-gateway-020: a rate-limiter failure fails closed (429) ────────────────────
def test_rate_limit_failure_is_429():
    r = run(_gw(limiter=_Limiter(boom=True)).handle(_make_request()))
    assert r.status_code == 429
