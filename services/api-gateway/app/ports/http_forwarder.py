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
    """Upstream unreachable / timed out."""
