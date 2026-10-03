"""HttpForwarder port — proxy a request to an upstream and return its response."""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class HttpForwarder(Protocol):
    driver: str

    async def forward(
        self, method: str, url: str, headers: dict[str, str],
        body: bytes, params: list[tuple[str, str]],
    ) -> tuple[int, dict[str, str], bytes]:
        """Return (status, response_headers, response_body). Raises UpstreamError on failure."""
        ...

    async def close(self) -> None: ...


class UpstreamError(Exception):
    """Upstream unreachable / timed out. `timeout` distinguishes an actual upstream TIMEOUT (→ 504)
    from a generic connect/transport failure (→ 502) so the two are not conflated (api-gateway-012)."""

    def __init__(self, message: str = "", *, timeout: bool = False) -> None:
        super().__init__(message)
        self.timeout = timeout
