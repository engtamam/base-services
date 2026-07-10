"""Opaque pagination cursor.

Offset pagination (`skip N`) drifts when the index changes between page requests — rows get
skipped or duplicated. A cursor instead encodes the *position* of the last result (its score
and id), so the next page is "everything ranked after this exact point", stable regardless of
inserts/deletes elsewhere. Results are ordered by (score desc, id asc) — the id tie-break
makes the order total and therefore pageable.
"""
from __future__ import annotations

import base64
import json


def encode(score: float, doc_id: str) -> str:
    # NOT rounded: the score must round-trip exactly, or `after()` (which compares the
    # decoded score against the hit's full-precision score) would drop the whole next page.
    raw = json.dumps({"s": float(score), "i": doc_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode(cursor: str) -> tuple[float, str] | None:
    if not cursor:
        return None
    try:
        pad = "=" * (-len(cursor) % 4)
        obj = json.loads(base64.urlsafe_b64decode(cursor + pad).decode())
        return float(obj["s"]), str(obj["i"])
    except Exception:
        return None                       # a malformed cursor → start from the top, never crash


def after(score: float, doc_id: str, position: tuple[float, str]) -> bool:
    """Is (score, id) strictly after `position` in (score desc, id asc) order?"""
    ps, pi = position
    if score < ps:
        return True
    if score > ps:
        return False
    return doc_id > pi
