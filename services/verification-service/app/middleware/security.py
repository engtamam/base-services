# =============================================================================
# verification-service — Security & hardening middleware
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


class BodyLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests whose declared body exceeds the configured limit."""

    def __init__(self, app, *, max_bytes: int) -> None:
        super().__init__(app)
        self._max = max_bytes

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                return JSONResponse({"error": "BAD_REQUEST"}, status_code=400)
            if declared > self._max:
                return JSONResponse(
                    {"error": "PAYLOAD_TOO_LARGE"}, status_code=413
                )
        return await call_next(request)


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
