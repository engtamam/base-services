"""Object-key validation shared by all ObjectStore adapters (CWE-22 defense)."""
from __future__ import annotations

import posixpath
import re

from app.ports.errors import PortError

# Safe object keys: forward-slash paths of bounded, safe segments. No leading
# slash, no traversal, no NUL, no backslashes, no drive letters.
_SEGMENT = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def validate_key(key: str) -> str:
    if not key or len(key) > 1024:
        raise PortError("invalid object key: empty or too long")
    if "\x00" in key or "\\" in key:
        raise PortError("invalid object key: illegal character")
    if key.startswith("/") or key.startswith("~"):
        raise PortError("invalid object key: absolute path not allowed")
    segments = key.split("/")
    for seg in segments:
        if seg in ("", ".", "..") or not _SEGMENT.match(seg):
            raise PortError("invalid object key: unsafe segment")
    # Normalize and re-check it did not escape.
    normalized = posixpath.normpath(key)
    if normalized.startswith("..") or normalized.startswith("/"):
        raise PortError("invalid object key: path traversal")
    return normalized
