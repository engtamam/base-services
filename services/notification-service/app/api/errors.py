# =============================================================================
# notification-service — Safe error handling
# -----------------------------------------------------------------------------
# Clients receive GENERIC error codes only — never an exception message or stack
# trace (CWE-209: information exposure through error messages). Full detail is
# logged server-side (with redaction) under the request id.
# =============================================================================
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from app.config import get_settings
from app.middleware.request_context import get_request_id
from app.middleware.security import apply_security_headers, build_security_headers
from app.observability.logging import get_logger

_logger = get_logger("app.errors")

# Map well-known status codes to stable, generic client-facing error codes.
# Plain integers (not status.* constants) to avoid deprecation/version coupling.
_CODE_BY_STATUS: dict[int, str] = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    408: "REQUEST_TIMEOUT",
    413: "PAYLOAD_TOO_LARGE",
    422: "VALIDATION_ERROR",
    429: "RATE_LIMITED",
}


def register_error_handlers(app: FastAPI) -> None:
    settings = get_settings()
    _sec_headers = build_security_headers(
        frame_ancestors=settings.ALLOWED_FRAME_ANCESTORS,
        is_production=settings.is_production,
    )

    def _json(code: str, status_code: int) -> JSONResponse:
        body: dict[str, str] = {"error": code}
        rid = get_request_id()
        if rid:
            body["request_id"] = rid
        resp = JSONResponse(body, status_code=status_code)
        # Catch-all 500s are produced OUTSIDE the header middleware, so apply the
        # hardened headers here too — every response is hardened, no exceptions.
        apply_security_headers(resp, _sec_headers)
        return resp

    @app.exception_handler(StarletteHTTPException)
    async def _http_exc(request: Request, exc: StarletteHTTPException):
        code = _CODE_BY_STATUS.get(exc.status_code, "ERROR")
        if exc.status_code >= 500:
            _logger.error("http_exception", extra={"status": exc.status_code})
            code = "INTERNAL_ERROR"
        return _json(code, exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation_exc(request: Request, exc: RequestValidationError):
        # Do NOT echo the raw invalid input back (avoid reflected data / leaks).
        return _json("VALIDATION_ERROR", 422)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        # Log full type server-side; return a generic body to the client.
        _logger.error("unhandled_exception", extra={"exc_type": type(exc).__name__})
        return _json("INTERNAL_ERROR", 500)
