"""Core gateway — match, enforce auth at the edge, inject trusted headers, forward.

Trust model: the gateway verifies the JWT once and injects identity headers
(X-User-Id / X-Tenant-Id / X-Caps) plus the shared X-Internal-Token that lets
upstreams trust them. Client-supplied copies of those headers are STRIPPED so a
caller can never spoof identity. The original Authorization header is still
forwarded so upstreams may re-verify (defense in depth).
"""
from __future__ import annotations

import base64
import json
import logging
from urllib.parse import urlsplit

_log = logging.getLogger("app.core.gateway")

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.capmatch import holds_any
from app.middleware.request_context import get_request_id
from app.ports.errors import InvalidToken
from app.ports.http_forwarder import UpstreamError

# Headers a client must never set (anti-spoofing) + hop-by-hop (not forwarded).
_STRIP_FROM_CLIENT = frozenset({"x-internal-token", "x-user-id", "x-tenant-id", "x-caps"})
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
})


def _targets_internal(p: str) -> bool:
    """True if a path addresses the internal tier — any `internal` segment. The internal tier is
    mounted at /internal/… in every service, and `internal` is not a public resource name here, so a
    segment match is the robust, config-independent test (catches /internal/…, and a rewrite that
    lands on …/internal/…)."""
    return "internal" in [seg for seg in p.split("/") if seg]


def _err(code: str, status: int) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status)


def _abnormal_path(request: Request) -> bool:
    """True if the request path is a dot-segment / encoded-traversal form that would NORMALIZE
    differently downstream than it reads here. The gateway decides routing/auth/caps/rate-limit on
    request.url.path, but httpx COLLAPSES '.'/'..' when it builds the upstream URL — so `/public/../admin`
    could be authorized as the public route yet fetched as the admin one (auth bypass, api-gateway-r2-01).
    Rather than re-implement RFC-3986 collapsing, we REJECT any such path: a '.'/'..' segment, or an
    encoded dot/slash/backslash (%2e/%2f/%5c) in the raw target, so the decided path is byte-for-byte
    what is forwarded."""
    raw = request.scope.get("raw_path") or b""
    raw_target = (bytes(raw).decode("latin-1", "replace") if raw else request.url.path)
    low = raw_target.split("?", 1)[0].lower()
    if "%2e" in low or "%2f" in low or "%5c" in low:
        return True
    return any(seg in (".", "..") for seg in request.url.path.split("/"))


class Gateway:
    def __init__(self, *, route_table, forwarder, token_verifier, rate_limiter, cache,
                 internal_token: str, default_cache_ttl: int, policy=None,
                 trusted_proxy_hops: int = 0) -> None:
        self._routes = route_table
        self._forwarder = forwarder
        self._verifier = token_verifier
        self._limiter = rate_limiter
        self._cache = cache
        self._internal_token = internal_token
        self._default_ttl = default_cache_ttl
        self._policy = policy                      # the per-route/subject/tenant rate-limit policy
        self._trusted_hops = max(0, int(trusted_proxy_hops))

    async def handle(self, request: Request) -> Response:
        # Reject dot-segment / encoded-traversal paths BEFORE any routing/auth/rate-limit decision, so
        # the path we authorize is exactly the path forwarded (api-gateway-r2-01).
        if _abnormal_path(request):
            return _err("BAD_REQUEST", 400)
        path = request.url.path
        # THE EDGE NEVER PROXIES THE INTERNAL TIER — unconditionally, whatever the route table says.
        # The gateway injects the fleet's internal token into every forwarded request, so a single
        # route-table line pointing at an /internal/ path would put the public internet one edit away
        # from /internal/operations/recipient (a pseudonym -> real phone number). This refuses the
        # internal prefix as a PROPERTY OF THE CODE, not of a YAML file, and answers 404 so the surface
        # is not even confirmed to exist. Checked BEFORE the match, so no route can opt out of it.
        if _targets_internal(path):
            return _err("NOT_FOUND", 404)
        route = self._routes.match(path)
        if route is None:
            return _err("NOT_FOUND", 404)
        if not route.allows_method(request.method):
            return _err("METHOD_NOT_ALLOWED", 405)

        # ── identity (resolved BEFORE the limit, so it can key by subject/tenant) ─────────────
        # No 401 here yet: an invalid/absent token becomes identity=None and the `required` check
        # below turns it into a 401. Resolving first lets the rate limit bucket by the real caller.
        identity: dict[str, str] | None = None
        auth_header = request.headers.get("authorization", "")
        if route.auth in ("required", "optional") and auth_header.startswith("Bearer "):
            try:
                # OFFLOAD: the gateway verifies on EVERY proxied request, and verify() is synchronous
                # blocking work on jwks/rs256 (a JWKS fetch + RSA verify). Inline on this single-worker
                # loop it stalls every other in-flight proxy — the edge is the worst place to block.
                claims = await run_in_threadpool(self._verifier.verify, auth_header[7:])
                identity = self._identity_from(claims)
            except Exception:
                identity = None

        # ── rate limit (policy-driven: per-route class/limit/cost, composite key, real client IP) ──
        client_ip = self._client_ip(request)
        try:
            if self._policy is not None:
                cls = self._policy.classify(path)
                rl_key = self._policy.key(
                    cls, subject=(identity.get("sub") if identity else None),
                    ip=client_ip, tenant=(identity.get("tenant") if identity else None))
                verdict = await self._limiter.check(
                    rl_key, cost=cls.cost, limit=cls.limit, window_s=cls.window_s)
                if not verdict.allowed:
                    return _err("RATE_LIMITED", 429)
            else:
                if not await self._limiter.allow(f"ip:{client_ip}"):
                    return _err("RATE_LIMITED", 429)
        except Exception as exc:
            _log.error("rate_limit_failed_closed", extra={"error": type(exc).__name__,
                                                          "request_id": get_request_id(), "path": path})
            return _err("RATE_LIMITED", 429)  # fail-closed (limiter outage) — logged so a logic bug is visible

        # ── auth enforcement (after the limit, so a garbage-token flood is bounded) ───────────
        if route.auth == "required":
            if identity is None:
                return _err("UNAUTHORIZED", 401)
            held = set((identity.get("caps") or "").split(",")) if identity.get("caps") else set()
            # ANY-OF + wildcard, via the SAME matcher the admin/app catalogs use — so a caller the
            # catalog shows an action to is not then 403'd here. The old `issubset` was AND (hold EVERY
            # listed cap) with no wildcard, so a caller holding a broader/alternative cap (e.g.
            # platform.audit, or a platform.* grant) was wrongly refused on a route listing more caps.
            if not holds_any(held, route.capabilities):
                return _err("FORBIDDEN", 403)

        # ── response cache (safe GETs) ───────────────────────────────────────
        # ONLY cache truly shareable responses: an ANONYMOUS caller (identity is None) on a
        # non-required route. On an auth=optional route the gateway forwards X-User-Id/X-Tenant-Id and
        # the upstream returns per-user data — caching that under an identity-less key (and replaying it
        # to anyone, Set-Cookie and all) leaks one caller's data to the next. So a request that CARRIES
        # an identity never reads or writes the shared cache.
        ttl = route.cache_ttl or self._default_ttl
        cacheable = (request.method == "GET" and route.auth != "required"
                     and identity is None and ttl > 0)
        cache_key = f"gw:{request.method}:{path}?{request.url.query}"
        if cacheable:
            hit = await self._cache.get(cache_key)
            if hit:
                return _decode_cached(hit)

        # ── forward ──────────────────────────────────────────────────────────
        headers = self._forward_headers(request, identity)
        url = route.build_url(path)
        # Belt AND braces: even if a route REWRITES a public prefix onto an internal upstream path,
        # the internal-token'd request must not reach it. The incoming-path guard above catches the
        # direct case; this catches a rewrite.
        if _targets_internal(urlsplit(url).path):
            return _err("NOT_FOUND", 404)
        body = await request.body()
        params = list(request.query_params.multi_items())
        try:
            status, resp_headers, resp_body = await self._forwarder.forward(
                request.method, url, headers, body, params)
        except UpstreamError:
            return _err("BAD_GATEWAY", 504)

        if cacheable and 200 <= status < 300 and _is_storable(resp_headers):
            await self._cache.set(cache_key, _encode_cached(status, resp_headers, resp_body), ttl)
        return Response(content=resp_body, status_code=status, headers=resp_headers)

    # ── helpers ──────────────────────────────────────────────────────────────
    def _client_ip(self, request: Request) -> str:
        """The real client IP. Behind `trusted_proxy_hops` reverse proxies the socket peer is the
        LAST proxy, so the client is read from X-Forwarded-For that many entries from the right — and
        only those rightmost entries are trusted (a client can forge the leftmost ones). 0 hops trusts
        no header and uses the socket peer, so a misconfiguration fails toward over-bucketing, never
        toward trusting a spoofed IP."""
        if self._trusted_hops > 0:
            xff = request.headers.get("x-forwarded-for", "")
            chain = [p.strip() for p in xff.split(",") if p.strip()]
            if chain:
                idx = len(chain) - self._trusted_hops
                idx = min(max(idx, 0), len(chain) - 1)
                return chain[idx]
        return request.client.host if request.client else "unknown"

    def _identity_from(self, claims: dict) -> dict[str, str]:
        caps = claims.get("caps")
        return {
            "sub": str(claims.get("sub", "")),
            "tenant": str(claims.get("tenant") or claims.get("org") or ""),
            "caps": ",".join(caps) if isinstance(caps, (list, tuple)) else "",
        }

    def _forward_headers(self, request: Request, identity: dict | None) -> dict[str, str]:
        out: dict[str, str] = {}
        for k, v in request.headers.items():
            lk = k.lower()
            if lk in _HOP_BY_HOP or lk in _STRIP_FROM_CLIENT:
                continue  # drop hop-by-hop + any client-supplied identity/internal header
            out[k] = v
        out["X-Request-Id"] = get_request_id()
        out["X-Internal-Token"] = self._internal_token  # upstreams trust gateway
        if identity:
            out["X-User-Id"] = identity["sub"]
            out["X-Tenant-Id"] = identity["tenant"]
            out["X-Caps"] = identity["caps"]
        return out


def _is_storable(headers: dict[str, str]) -> bool:
    """Honor the upstream's OWN caching directives before storing a shared copy. A response the
    upstream marked no-store/private/no-cache, or that carries a Set-Cookie (per-user), or that
    Varies on a request header (we key only on method+path+query), must NOT be shared — caching it
    silently overrides the upstream's explicit instruction and can leak per-user data."""
    lower = {k.lower(): v for k, v in headers.items()}
    if "set-cookie" in lower:
        return False
    if lower.get("vary", "").strip():
        return False
    cc = lower.get("cache-control", "").lower()
    return not any(d in cc for d in ("no-store", "private", "no-cache"))


def _encode_cached(status: int, headers: dict[str, str], body: bytes) -> str:
    return json.dumps({"status": status, "headers": headers,
                       "body": base64.b64encode(body).decode()})


def _decode_cached(raw: str) -> Response:
    d = json.loads(raw)
    return Response(content=base64.b64decode(d["body"]), status_code=d["status"],
                    headers={**d.get("headers", {}), "X-Gateway-Cache": "HIT"})
