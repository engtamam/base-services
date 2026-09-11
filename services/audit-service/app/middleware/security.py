# =============================================================================
# audit-service — Security & hardening middleware
# -----------------------------------------------------------------------------
#   SecurityHeadersMiddleware — CSP, HSTS, Permissions-Policy, nosniff, referrer,
#                               frame-ancestors; hides server banner.
#   BodyLimitMiddleware       — rejects oversized bodies (CWE-400 resource abuse).
#   TimeoutMiddleware         — aborts hung requests (CWE-400 / availability).
# =============================================================================
from __future__ import annotations

import asyncio

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


def build_security_headers(*, frame_ancestors: str, is_production: bool) -> dict[str, str]:
    """The hardened header set. Single source of truth so both the middleware
    and the error handlers (which run OUTSIDE the middleware for catch-all 500s)
    apply an identical set."""
    headers = {
        "Content-Security-Policy": (
            "default-src 'none'; base-uri 'none'; form-action 'none'; "
            f"frame-ancestors {frame_ancestors}"
        ),
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "same-origin",
    }
    # HSTS only makes sense over TLS (production edge).
    if is_production:
        headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"
    return headers


def apply_security_headers(response: Response, headers: dict[str, str]) -> None:
    for key, value in headers.items():
        response.headers[key] = value
    # Strip any server-identifying banner (info disclosure, CWE-200).
    if "server" in response.headers:
        del response.headers["server"]


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach hardened security headers to every response."""

    def __init__(self, app, *, frame_ancestors: str, is_production: bool) -> None:
        super().__init__(app)
        self._headers = build_security_headers(
            frame_ancestors=frame_ancestors, is_production=is_production
        )

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        response: Response = await call_next(request)
        apply_security_headers(response, self._headers)
        return response


class BodyLimitMiddleware:
    """Reject requests whose body exceeds the configured limit (CWE-400).

    PURE ASGI, deliberately — not BaseHTTPMiddleware. A Content-Length check alone is bypassable: a
    Transfer-Encoding: chunked request carries no Content-Length, so the header check sees nothing and
    an attacker streams an unbounded body straight through. To cap a chunked body we must count the
    bytes as they arrive AND still hand the body to the downstream app. BaseHTTPMiddleware cannot do
    the second half (a receive it swaps in does not reach the wrapped app via call_next), so this is a
    pure-ASGI middleware that owns the receive channel it passes downstream.
    """

    def __init__(self, app, *, max_bytes: int) -> None:
        self.app = app
        self._max = max_bytes

    @staticmethod
    async def _reject(scope, send, status: int, error: str) -> None:
        async def _noop_receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        await JSONResponse({"error": error}, status_code=status)(scope, _noop_receive, send)

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        headers = {k.lower(): v for k, v in (scope.get("headers") or [])}
        cl = headers.get(b"content-length")
        if cl is not None:
            try:
                declared = int(cl)
            except ValueError:
                return await self._reject(scope, send, 400, "BAD_REQUEST")
            if declared > self._max:
                return await self._reject(scope, send, 413, "PAYLOAD_TOO_LARGE")
            return await self.app(scope, receive, send)
        # No Content-Length (chunked/streamed): buffer while counting, bail the moment the cap is
        # crossed (so we never hold more than the limit), then replay the buffered body downstream.
        body = b""
        while True:
            message = await receive()
            if message["type"] != "http.request":
                # e.g. http.disconnect — nothing more to read; hand what we have to the app.
                async def _forward(_m=message):
                    return _m
                return await self.app(scope, _forward, send)
            body += message.get("body", b"")
            if len(body) > self._max:
                return await self._reject(scope, send, 413, "PAYLOAD_TOO_LARGE")
            if not message.get("more_body", False):
                break

        replayed = False

        async def _replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        return await self.app(scope, _replay, send)


class TimeoutMiddleware(BaseHTTPMiddleware):
    """Abort a request that exceeds the configured wall-clock budget."""

    def __init__(self, app, *, timeout_ms: int) -> None:
        super().__init__(app)
        self._timeout_s = timeout_ms / 1000.0

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        try:
            return await asyncio.wait_for(call_next(request), timeout=self._timeout_s)
        except asyncio.TimeoutError:
            return JSONResponse({"error": "REQUEST_TIMEOUT"}, status_code=408)
