"""Magic-byte scanner — blocks executables and content that lies about its type.

Zero-dep heuristic defense against unrestricted file upload (CWE-434 / CWE-646):
  * reject known-executable signatures regardless of declared type
  * reject active (markup/script) content by declared type AND by leading bytes
  * when strict, require the bytes to match the declared content-type's signature, and fail closed on
    an unverifiable (unknown) declared type

file-006: this is a HEADER-ONLY heuristic — it inspects only the leading bytes, so a dangerous
signature that is NOT at the start (e.g. buried in a polyglot) is invisible to it. It is defence in
depth, not a content scanner. For the LARGE (multi-GB, direct-to-bucket) path a real content scanner
is required in production (SCANNER_DRIVER=clamav); assert_large_scan_ok refuses async/require with no
scanner wired. Do not rely on a 16/64-byte sniff to satisfy a large-upload scan policy.
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

# file-005: ACTIVE content types carry executable markup (script, event handlers, external entities)
# and are rejected OUTRIGHT, whatever the bytes. A browser that is tricked into rendering one of these
# runs attacker script in the app's origin (stored XSS) — the classic image-upload-is-actually-SVG attack.
_ACTIVE_CONTENT_TYPES: frozenset[str] = frozenset({
    "image/svg+xml", "text/html", "application/xhtml+xml", "text/xml",
    "application/xml", "text/javascript", "application/javascript", "application/x-javascript",
})
# Leading markup that betrays active content REGARDLESS of the declared type (a .png that is really an
# SVG/HTML document). Matched against a lowercased, whitespace/BOM-stripped header.
_ACTIVE_MARKERS: tuple[bytes, ...] = (b"<svg", b"<!doctype html", b"<html", b"<script", b"<?xml")
# How many leading bytes we inspect — enough to see a markup root past a short prolog.
_HEADER_BYTES = 64


class MagicScanner:
    driver = "magic"

    def __init__(self, strict: bool = True) -> None:
        self._strict = strict

    def scan(self, data: bytes, content_type: str) -> None:
        self._check(data[:_HEADER_BYTES], content_type)

    async def scan_stream(self, chunks: AsyncIterator[bytes], content_type: str) -> None:
        # Magic sniffing needs only the leading bytes, but we must DRAIN the whole stream so the
        # caller's hasher sees every byte. Keep a bounded header; discard the rest as it flows by.
        header = b""
        async for chunk in chunks:
            if len(header) < _HEADER_BYTES:
                header += chunk[: _HEADER_BYTES - len(header)]
        self._check(header, content_type)

    def _check(self, head: bytes, content_type: str) -> None:
        for sig in _DANGEROUS:
            if head.startswith(sig):
                raise Rejected("executable/script content is not allowed")
        ct = (content_type or "").split(";")[0].strip().lower()
        # Reject active types by DECLARATION...
        if ct in _ACTIVE_CONTENT_TYPES:
            raise Rejected(f"active content type is not allowed: {ct}")
        # ...and by CONTENT, so a .png that is really SVG/HTML cannot slip through.
        probe = head.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
        if any(probe.startswith(m) for m in _ACTIVE_MARKERS):
            raise Rejected("active (markup/script) content is not allowed")
        expected = _SIGNATURES.get(ct)
        if self._strict and expected and not any(head.startswith(s) for s in expected):
            raise Rejected(f"content does not match declared type: {ct}")
        # file-005: in strict mode an UNKNOWN declared type cannot be verified by signature — fail
        # closed. The content-type allowlist (ALLOWED_CONTENT_TYPES) is the production gate; this is
        # defence in depth so an unlisted type never reaches storage unverified when strict is on.
        if self._strict and ct and expected is None:
            raise Rejected(f"unverifiable content type (no signature): {ct}")
