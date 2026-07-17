"""ClamAV scanner — infra-gated (needs a clamd daemon + the 'clamd' package)."""
from __future__ import annotations

import asyncio
import tempfile
from typing import AsyncIterator

from app.adapters.lazy import load
from app.ports.errors import ConfigError
from app.ports.scanner import Rejected


class ClamavScanner:
    driver = "clamav"

    def __init__(self, host: str = "", port: int = 3310) -> None:
        clamd = load("clamd", "clamav")
        try:
            self._client = (clamd.ClamdNetworkSocket(host, port) if host
                            else clamd.ClamdUnixSocket())
        except Exception as exc:  # pragma: no cover - needs a daemon
            raise ConfigError(f"clamd unavailable: {exc}") from exc

    def scan(self, data: bytes, content_type: str) -> None:  # pragma: no cover - needs a daemon
        import io

        result = self._client.instream(io.BytesIO(data))
        status = result.get("stream", ("OK",))[0]
        if status != "OK":
            raise Rejected("malware detected")

    async def scan_stream(self, chunks: AsyncIterator[bytes],
                          content_type: str) -> None:  # pragma: no cover - needs a daemon
        # Spool the streamed object to a disk-backed temp file (bounded memory even for a multi-GB
        # object), then INSTREAM it to clamd. clamd itself streams from the file handle.
        spool = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
        try:
            async for chunk in chunks:
                await asyncio.to_thread(spool.write, chunk)
            await asyncio.to_thread(spool.seek, 0)
            result = await asyncio.to_thread(self._client.instream, spool)
            status = result.get("stream", ("OK",))[0]
            if status != "OK":
                raise Rejected("malware detected")
        finally:
            await asyncio.to_thread(spool.close)
