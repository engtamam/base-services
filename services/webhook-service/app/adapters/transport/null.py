"""Null transport — delivers nothing, reports a transport failure (fail-safe, retryable)."""
from __future__ import annotations

from app.ports.webhook_transport import Response


class NullTransport:
    driver = "null"

    def send(self, *, url, pinned_ip, body, headers, timeout_seconds, max_response_bytes) -> Response:
        return Response(ok=False, status=0, error="no transport configured")
