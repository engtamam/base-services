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

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.middleware.request_context import get_request_id
from app.ports.errors import InvalidToken
from app.ports.http_forwarder import UpstreamError

# Headers a client must never set (anti-spoofing) + hop-by-hop (not forwarded).
_STRIP_FROM_CLIENT = frozenset({"x-internal-token", "x-user-id", "x-tenant-id", "x-caps"})
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
})


def _err(code: str, status: int) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status)


class Gateway:
    def __init__(self, *, route_table, forwarder, token_verifier, rate_limiter, cache,
                 internal_token: str, default_cache_ttl: int) -> None:
        self._routes = route_table
        self._forwarder = forwarder
        self._verifier = token_verifier
        self._limiter = rate_limiter
        self._cache = cache
        self._internal_token = internal_token
        self._default_ttl = default_cache_ttl

    async def handle(self, request: Request) -> Response:
        path = request.url.path
        route = self._routes.match(path)
        if route is None:
            return _err("NOT_FOUND", 404)
        if not route.allows_method(request.method):
            return _err("METHOD_NOT_ALLOWED", 405)

        # ── rate limit (edge) ────────────────────────────────────────────────
        client = request.client
        rl_key = f"ip:{client.host}" if client else "ip:unknown"
        try:
            if not await self._limiter.allow(rl_key):
                return _err("RATE_LIMITED", 429)
        except Exception:
            return _err("RATE_LIMITED", 429)  # fail-closed

        # ── auth (edge) + identity ───────────────────────────────────────────
        identity: dict[str, str] | None = None
        auth_header = request.headers.get("authorization", "")
        if route.auth in ("required", "optional") and auth_header.startswith("Bearer "):
            try:
                claims = self._verifier.verify(auth_header[7:])
                identity = self._identity_from(claims)
            except InvalidToken:
                if route.auth == "required":
                    return _err("INVALID_TOKEN", 401)
        if route.auth == "required":
            if identity is None:
                return _err("UNAUTHORIZED", 401)
            held = set((identity.get("caps") or "").split(",")) if identity.get("caps") else set()
            if route.capabilities and not set(route.capabilities).issubset(held):
                return _err("FORBIDDEN", 403)

        # ── response cache (safe GETs) ───────────────────────────────────────
        ttl = route.cache_ttl or self._default_ttl
        cacheable = request.method == "GET" and route.auth != "required" and ttl > 0
        cache_key = f"gw:{request.method}:{path}?{request.url.query}"
        if cacheable:
            hit = await self._cache.get(cache_key)
            if hit:
                return _decode_cached(hit)

        # ── forward ──────────────────────────────────────────────────────────
        headers = self._forward_headers(request, identity)
        url = route.build_url(path)
        body = await request.body()
        params = list(request.query_params.multi_items())
        try:
            status, resp_headers, resp_body = await self._forwarder.forward(
                request.method, url, headers, body, params)
        except UpstreamError:
            return _err("BAD_GATEWAY", 504)

        if cacheable and 200 <= status < 300:
            await self._cache.set(cache_key, _encode_cached(status, resp_headers, resp_body), ttl)
        return Response(content=resp_body, status_code=status, headers=resp_headers)

    # ── helpers ──────────────────────────────────────────────────────────────
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


def _encode_cached(status: int, headers: dict[str, str], body: bytes) -> str:
    return json.dumps({"status": status, "headers": headers,
                       "body": base64.b64encode(body).decode()})


def _decode_cached(raw: str) -> Response:
    d = json.loads(raw)
    return Response(content=base64.b64decode(d["body"]), status_code=d["status"],
                    headers={**d.get("headers", {}), "X-Gateway-Cache": "HIT"})
