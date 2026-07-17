"""Scanner port — reject malicious / disallowed uploads (CWE-434 defense)."""
from __future__ import annotations

from typing import AsyncIterator, Protocol, runtime_checkable

from app.ports.errors import PortError


class Rejected(PortError):
    """The upload was rejected (malware, disallowed/mismatched type)."""


@runtime_checkable
class Scanner(Protocol):
    driver: str

    def scan(self, data: bytes, content_type: str) -> None:
        """Return None if clean; raise Rejected otherwise. The SMALL path (bytes in memory)."""
        ...

    async def scan_stream(self, chunks: AsyncIterator[bytes], content_type: str) -> None:
        """Scan a LARGE object streamed in chunks — return None if clean, raise Rejected otherwise.

        The implementation MUST consume the whole iterator (the caller hashes the same stream as a
        side effect, so a scanner that stops early would leave the object only partly verified). A
        multi-GB object is never held whole: header-only scanners keep a bounded prefix, and a real
        AV streams to its daemon.
        """
        ...
