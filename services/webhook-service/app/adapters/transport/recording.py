"""Recording transport — no network. Records what WOULD be sent (pilot / staging / tests)."""
from __future__ import annotations

from app.ports.webhook_transport import Response


class RecordingTransport:
    driver = "recording"

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, *, url, pinned_ip, body, headers, timeout_seconds, max_response_bytes) -> Response:
        self.sent.append({"url": url, "ip": pinned_ip, "body": body, "headers": dict(headers)})
        return Response(ok=True, status=200, body="", latency_ms=0)
