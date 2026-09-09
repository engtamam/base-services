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
                # STREAM the response and stop reading past the cap. `client.post(...).text` buffers
                # the WHOLE body into memory first and truncates only afterwards, so a hostile or
                # broken receiver returning a multi-GB body OOMs the worker before the cap is ever
                # applied. iter_raw caps the WIRE bytes (bounded memory regardless of content-
                # encoding — a decoded/decompressed stream could expand a bomb), then we decode the
                # capped slice for the diagnostic body. Mirrors the stdlib transport's read cap.
                with client.stream("POST", pinned, content=body,
                                   headers={**headers, "Host": host},
                                   extensions={"sni_hostname": host}) as resp:
                    raw = bytearray()
                    for chunk in resp.iter_raw():
                        raw.extend(chunk)
                        if len(raw) > max_response_bytes:
                            break                         # enough to store; do not read the rest
                    latency = int((time.monotonic() - started) * 1000)
                    return Response(ok=True, status=resp.status_code,
                                    body=bytes(raw[:max_response_bytes]).decode("utf-8", "replace"),
                                    latency_ms=latency,
                                    headers={k.lower(): v for k, v in resp.headers.items()})
        except Exception as exc:
            return Response(ok=False, status=0, error=type(exc).__name__,
                            latency_ms=int((time.monotonic() - started) * 1000))
