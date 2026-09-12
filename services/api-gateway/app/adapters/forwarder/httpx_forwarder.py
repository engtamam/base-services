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

    def __init__(self, timeout: int = 15, max_response_bytes: int = 0) -> None:
        self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
        self._max = max(0, int(max_response_bytes))

    async def forward(self, method, url, headers, body, params):
        # STREAM the upstream response and enforce a size cap: reading resp.content buffered an
        # unbounded body into RAM (api-gateway-r2-05). We reject early on a too-large Content-Length,
        # and abort mid-stream once the cap is exceeded, so memory is bounded either way.
        try:
            req = self._client.build_request(method, url, headers=headers, content=body, params=params)
            resp = await self._client.send(req, stream=True)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise UpstreamError(str(exc)) from exc
        try:
            cl = resp.headers.get("content-length")
            if self._max and cl and cl.isdigit() and int(cl) > self._max:
                raise UpstreamError(f"upstream response exceeds {self._max} bytes")
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if self._max and total > self._max:
                    raise UpstreamError(f"upstream response exceeds {self._max} bytes")
                chunks.append(chunk)
            content = b"".join(chunks)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise UpstreamError(str(exc)) from exc
        finally:
            await resp.aclose()
        out = {k: v for k, v in resp.headers.items() if k.lower() not in _HOP_BY_HOP}
        return resp.status_code, out, content

    async def close(self) -> None:
        await self._client.aclose()
