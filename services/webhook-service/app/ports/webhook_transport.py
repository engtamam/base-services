"""WebhookTransport port — make the outbound request to a PRE-VALIDATED, PINNED IP.

The transport never resolves DNS itself: `core.ssrf` resolves + validates the host and
hands over the exact IP to connect to. That closes the TOCTOU / DNS-rebinding window
between "we checked the URL" and "we opened the socket" (CWE-918).
Response bodies are read up to `max_response_bytes` only — a customer must not be able to
stream us a 1 GB body.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class Response:
    ok: bool                       # transport-level success (we got an HTTP response)
    status: int = 0                # 0 = no response (timeout / DNS / connection error)
    body: str = ""                 # truncated to max_response_bytes
    latency_ms: int = 0
    error: str = ""
    headers: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class WebhookTransport(Protocol):
    driver: str

    def send(self, *, url: str, pinned_ip: str, body: bytes, headers: dict[str, str],
             timeout_seconds: int, max_response_bytes: int) -> Response:
        """POST `body` to `url`, connecting only to `pinned_ip`. Never raises."""
        ...
