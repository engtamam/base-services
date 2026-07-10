"""httpx HttpForwarder — async proxy to an upstream. Strips hop-by-hop headers."""
from __future__ import annotations

import httpx

from app.ports.http_forwarder import UpstreamError

# Hop-by-hop headers must not be forwarded (RFC 7230).
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
})


class HttpxForwarder:
    driver = "httpx"

    def __init__(self, timeout: int = 15) -> None:
        self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)

    async def forward(self, method, url, headers, body, params):
        try:
            resp = await self._client.request(
                method, url, headers=headers, content=body, params=params
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise UpstreamError(str(exc)) from exc
        out = {k: v for k, v in resp.headers.items() if k.lower() not in _HOP_BY_HOP}
        return resp.status_code, out, resp.content

    async def close(self) -> None:
        await self._client.aclose()
