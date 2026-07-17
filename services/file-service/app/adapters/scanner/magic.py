"""Magic-byte scanner — blocks executables and content that lies about its type.

Zero-dep heuristic defense against unrestricted file upload (CWE-434 / CWE-646):
  * reject known-executable signatures regardless of declared type
  * when strict, require the bytes to match the declared content-type's signature
"""
from __future__ import annotations

from typing import AsyncIterator

from app.ports.scanner import Rejected

# Declared content-type -> acceptable leading magic bytes.
_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "image/webp": (b"RIFF",),
    "image/bmp": (b"BM",),
    "application/pdf": (b"%PDF",),
    "application/zip": (b"PK\x03\x04", b"PK\x05\x06"),
    "application/gzip": (b"\x1f\x8b",),
}
# Executable / dangerous signatures — always blocked.
_DANGEROUS: tuple[bytes, ...] = (b"MZ", b"\x7fELF", b"#!", b"\xca\xfe\xba\xbe")


class MagicScanner:
    driver = "magic"

    def __init__(self, strict: bool = True) -> None:
        self._strict = strict

    def scan(self, data: bytes, content_type: str) -> None:
        self._check(data[:16], content_type)

    async def scan_stream(self, chunks: AsyncIterator[bytes], content_type: str) -> None:
        # Magic sniffing needs only the leading bytes, but we must DRAIN the whole stream so the
        # caller's hasher sees every byte. Keep a bounded header; discard the rest as it flows by.
        header = b""
        async for chunk in chunks:
            if len(header) < 16:
                header += chunk[: 16 - len(header)]
        self._check(header, content_type)

    def _check(self, head: bytes, content_type: str) -> None:
        for sig in _DANGEROUS:
            if head.startswith(sig):
                raise Rejected("executable/script content is not allowed")
        ct = (content_type or "").split(";")[0].strip().lower()
        expected = _SIGNATURES.get(ct)
        if self._strict and expected and not any(head.startswith(s) for s in expected):
            raise Rejected(f"content does not match declared type: {ct}")
