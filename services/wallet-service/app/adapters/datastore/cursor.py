"""Opaque keyset pagination cursor.

`OFFSET n` is the wrong primitive twice over. It is O(n) in the database — the rows are read
and thrown away — and it is **unstable**: insert a row while a client pages, and page 2 silently
skips one. The client never learns it missed a file.

A keyset cursor instead encodes the *position* of the last row returned, so the next page is
"everything ordered after this exact point". It is O(1) with the right index and it cannot skip
or duplicate, whatever else is happening to the collection.

The ordering key is `(sort_value, id)`. The `id` tie-break is not decoration: without it, rows
sharing a `created` timestamp straddle a page boundary and are dropped or repeated. The order
must be TOTAL to be pageable.
"""
from __future__ import annotations

import base64
import binascii
import json
from typing import Any


def encode(sort_value: Any, row_id: str) -> str:
    """Opaque to the caller, so the format stays ours to change."""
    raw = json.dumps({"v": sort_value, "i": row_id}, separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode(cursor: str) -> tuple[Any, str] | None:
    """A malformed cursor is not an error to shout about — it is a client that lost its place.

    Returns None, and the caller starts from the beginning, rather than 500-ing on input a user
    can trivially corrupt.
    """
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        return data["v"], str(data["i"])
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeDecodeError):
        return None


def after(row_sort_value: Any, row_id: str, cursor: tuple[Any, str], descending: bool) -> bool:
    """Is this row strictly past the cursor, in the given direction?

    Compared as a TUPLE, so the id tie-break decides rows with an equal sort value.
    """
    last_value, last_id = cursor
    here = (_key(row_sort_value), row_id)
    there = (_key(last_value), last_id)
    return here < there if descending else here > there


def _key(value: Any) -> Any:
    """Order None consistently rather than raising when a row lacks the sort field."""
    return "" if value is None else value
