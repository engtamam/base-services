# =============================================================================
# job-service — Request-ID tracing
# -----------------------------------------------------------------------------
# Accepts an inbound X-Request-Id (validated) or generates a fresh UUID4, stores
# it in a contextvar so logs can attach it, and echoes it on the response.
# =============================================================================
from __future__ import annotations

import re
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

_REQUEST_ID: ContextVar[str] = ContextVar("request_id", default="")

# Only accept a sane inbound id (defends against header/log injection, CWE-117):
# printable ASCII, bounded length, safe charset.
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

HEADER = "X-Request-Id"


def get_request_id() -> str:
    return _REQUEST_ID.get()


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        incoming = request.headers.get(HEADER, "")
        request_id = incoming if _SAFE_ID.match(incoming) else uuid.uuid4().hex
        token = _REQUEST_ID.set(request_id)
        try:
            response: Response = await call_next(request)
        finally:
            _REQUEST_ID.reset(token)
        response.headers[HEADER] = request_id
        return response
