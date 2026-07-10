"""httpx transport — pins the IP via an explicit connection mapping. Optional [http] extra."""
from __future__ import annotations

import time
from urllib.parse import urlsplit

from app.adapters.lazy import load
from app.ports.webhook_transport import Response


class HttpxTransport:
    driver = "httpx"

    def __init__(self) -> None:
        self._httpx = load("httpx", "http")

    def send(self, *, url, pinned_ip, body, headers, timeout_seconds, max_response_bytes) -> Response:
        parts = urlsplit(url)
        host = parts.hostname or ""
        started = time.monotonic()
        try:
            # connect to the pinned IP while keeping Host/SNI = the original hostname
            transport = self._httpx.HTTPTransport(retries=0)
            pinned = url.replace(f"//{host}", f"//{pinned_ip}", 1)
            with self._httpx.Client(transport=transport, timeout=timeout_seconds,
                                    verify=True) as client:
                resp = client.post(pinned, content=body,
                                   headers={**headers, "Host": host},
                                   extensions={"sni_hostname": host})
                latency = int((time.monotonic() - started) * 1000)
                return Response(ok=True, status=resp.status_code,
                                body=resp.text[:max_response_bytes], latency_ms=latency,
                                headers={k.lower(): v for k, v in resp.headers.items()})
        except Exception as exc:
            return Response(ok=False, status=0, error=type(exc).__name__,
                            latency_ms=int((time.monotonic() - started) * 1000))
