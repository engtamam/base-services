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

    def __init__(self, status=200, body=b"OK", raise_error=False, resp_headers=None):
        self.status, self.body, self.raise_error = status, body, raise_error
        self._resp_headers = resp_headers or {"content-type": "text/plain"}
        self.last = None

    async def forward(self, method, url, headers, body, params):
        if self.raise_error:
            raise UpstreamError("boom")
        self.last = {"method": method, "url": url, "headers": headers}
        return self.status, dict(self._resp_headers), self.body

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
        self.checks = []          # (key, cost, limit, window_s) for each policy check

    async def allow(self, key):
        return self._allow

    async def check(self, key, *, cost=1, limit=None, window_s=None):
        from app.ports.rate_limiter import Verdict
        self.checks.append((key, cost, limit, window_s))
        return Verdict(allowed=self._allow, limit=limit or 0, remaining=0, retry_after_s=0, reset_s=0)


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


def test_internal_prefix_is_refused_even_when_a_route_matches_it():
    # Round-3 #4: the gateway injects the fleet internal token, so a route pointing at /internal/
    # would put the public internet one YAML edit from /internal/operations/recipient. The refusal is
    # a property of the code, not the route table — 404 even with a matching route, and BEFORE forward.
    fwd = FakeForwarder()
    r = Route(prefix="/internal", upstream="http://operations")
    assert run(_gw(r, forwarder=fwd).handle(make_request(path="/internal/operations/recipient"))).status_code == 404
    # a nested internal segment is caught too
    r2 = Route(prefix="/x", upstream="http://operations")
    assert run(_gw(r2).handle(make_request(path="/x/internal/erase"))).status_code == 404


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


def test_multi_capability_route_is_any_of_not_all_of():
    """A route listing >1 capability means ANY-OF: holding one is enough (was AND via issubset)."""
    r = Route(prefix="/echo", upstream="http://up", auth="required",
              capabilities=("audit.view", "platform.audit"))
    # Caller holds only the broader one — must be ALLOWED (the reported false-403).
    gw = _gw(r, forwarder=FakeForwarder(),
             verifier=FakeVerifier(claims={"sub": "u", "caps": ["platform.audit"]}))
    assert run(gw.handle(make_request(headers={"Authorization": "Bearer x"}))).status_code == 200


def test_wildcard_grant_satisfies_a_specific_capability():
    r = Route(prefix="/echo", upstream="http://up", auth="required", capabilities=("platform.audit",))
    gw = _gw(r, forwarder=FakeForwarder(),
             verifier=FakeVerifier(claims={"sub": "u", "caps": ["platform.*"]}))
    assert run(gw.handle(make_request(headers={"Authorization": "Bearer x"}))).status_code == 200


def test_multi_capability_route_denies_a_caller_holding_none():
    r = Route(prefix="/echo", upstream="http://up", auth="required",
              capabilities=("audit.view", "platform.audit"))
    gw = _gw(r, verifier=FakeVerifier(claims={"sub": "u", "caps": ["billing.read"]}))
    assert run(gw.handle(make_request(headers={"Authorization": "Bearer x"}))).status_code == 403


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


def test_upstream_generic_error_502():
    # api-gateway-012: a generic upstream failure is 502; 504 is reserved for an actual timeout.
    r = Route(prefix="/echo", upstream="http://up")
    gw = _gw(r, forwarder=FakeForwarder(raise_error=True))
    assert run(gw.handle(make_request())).status_code == 502


def test_cache_hit_second_call():
    r = Route(prefix="/echo", upstream="http://up", auth="none", cache_ttl=60)
    fwd = FakeForwarder(body=b"cached")
    cache = FakeCache()
    gw = _gw(r, forwarder=fwd, cache=cache)
    first = run(gw.handle(make_request()))
    assert first.status_code == 200 and "X-Gateway-Cache" not in first.headers
    second = run(gw.handle(make_request()))
    assert second.headers.get("X-Gateway-Cache") == "HIT"


# ── audit fixes (api-gateway-01/02/04/05) ────────────────────────────────────────────────
from app.core.ratelimit_policy import RateLimitPolicy  # noqa: E402


def _policy():
    return RateLimitPolicy.from_config(
        key_parts="subject,ip,route",
        routes_json='[{"prefix":"/echo","class":"echo","limit":5,"window_s":60,"cost":2}]',
        default_limit=100, default_window_s=60)


def test_policy_limit_uses_route_budget_and_the_real_client_ip():
    # audit #02: the proxy path must apply the per-route policy (limit/window/cost) and bucket by the
    # REAL client IP from X-Forwarded-For (trusted hops), not the shared socket/ingress IP.
    r = Route(prefix="/echo", upstream="http://up")
    lim = FakeLimiter(allow=True)
    gw = Gateway(route_table=FakeTable(r), forwarder=FakeForwarder(), token_verifier=FakeVerifier(),
                 rate_limiter=lim, cache=FakeCache(), internal_token="I", default_cache_ttl=0,
                 policy=_policy(), trusted_proxy_hops=1)
    run(gw.handle(make_request(headers={"X-Forwarded-For": "1.1.1.1, 2.2.2.2"})))
    assert lim.checks, "the policy check was not applied on the proxy path"
    key, cost, limit, window = lim.checks[0]
    assert (cost, limit, window) == (2, 5, 60)          # per-route budget honored
    assert "i:2.2.2.2" in key                            # real client (1 trusted hop = last entry)
    assert "1.1.1.1" not in key                          # the untrusted, spoofable left entry


def test_optional_route_with_identity_is_never_cached():
    # audit #01: an authenticated caller on an optional-auth route gets per-user data; it must not be
    # stored under the identity-less shared key (which would replay it to the next caller).
    r = Route(prefix="/echo", upstream="http://up", auth="optional", cache_ttl=60)
    cache = FakeCache()
    gw = _gw(r, verifier=FakeVerifier(claims={"sub": "u", "tenant": "t", "caps": ["read"]}),
             cache=cache, ttl=60)
    resp = run(gw.handle(make_request(headers={"Authorization": "Bearer x"})))
    assert resp.status_code == 200
    assert cache.data == {}                              # nothing shared


def test_optional_route_anonymous_is_cached():
    r = Route(prefix="/echo", upstream="http://up", auth="optional", cache_ttl=60)
    cache = FakeCache()
    gw = _gw(r, cache=cache, ttl=60)                     # no token → anonymous
    run(gw.handle(make_request()))
    assert cache.data != {}                              # anonymous responses may be shared


def test_response_with_set_cookie_is_not_cached():
    # audit #04: honor the upstream's own caching directives.
    r = Route(prefix="/echo", upstream="http://up", auth="none", cache_ttl=60)
    cache = FakeCache()
    fwd = FakeForwarder(resp_headers={"content-type": "text/plain", "set-cookie": "sid=1"})
    gw = _gw(r, forwarder=fwd, cache=cache, ttl=60)
    run(gw.handle(make_request()))
    assert cache.data == {}


def test_response_with_private_cache_control_is_not_cached():
    r = Route(prefix="/echo", upstream="http://up", auth="none", cache_ttl=60)
    cache = FakeCache()
    fwd = FakeForwarder(resp_headers={"cache-control": "private, max-age=0"})
    gw = _gw(r, forwarder=fwd, cache=cache, ttl=60)
    run(gw.handle(make_request()))
    assert cache.data == {}


def test_require_capability_honors_wildcard_grant():
    # audit #05: enforcement must be wildcard-aware like the catalog filter, or the panel shows an
    # action it then 403s. A held platform.* satisfies a required platform.audit.
    from app.api.auth import Principal
    from app.api.authz import require_capability
    dep = require_capability("platform.audit")
    holder = Principal(subject="u", caps=frozenset({"platform.*"}))
    assert run(dep(principal=holder)) is holder


class BoomVerifier:
    driver = "fake"
    def verify(self, token):
        raise RuntimeError("JWKS endpoint unreachable")


def test_verifier_outage_degrades_to_anonymous_on_optional_route():
    r = Route(prefix="/echo", upstream="http://up", auth="optional")
    resp = run(_gw(r, verifier=BoomVerifier()).handle(
        make_request(path="/echo/x", headers={"authorization": "Bearer tok"})))
    assert resp.status_code != 500                       # not a 500 from the verifier blip
    assert resp.status_code in (200, 204, 502)           # proceeds anonymously to the upstream


def test_verifier_outage_is_401_not_500_on_required_route():
    r = Route(prefix="/echo", upstream="http://up", auth="required")
    resp = run(_gw(r, verifier=BoomVerifier()).handle(
        make_request(path="/echo/x", headers={"authorization": "Bearer tok"})))
    assert resp.status_code == 401                       # identity=None → unauthorized, never 500
