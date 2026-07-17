"""No scanning — accept anything (default; pair with type/size validation)."""
from __future__ import annotations

from typing import AsyncIterator


class NoneScanner:
    driver = "none"

    def scan(self, data: bytes, content_type: str) -> None:
        return None

    async def scan_stream(self, chunks: AsyncIterator[bytes], content_type: str) -> None:
        # Drain the stream so the caller's hasher sees every byte; scan nothing.
        async for _ in chunks:
            pass
