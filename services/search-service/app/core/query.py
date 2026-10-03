"""Structured query validation + cost limits.

There is NO raw backend query string — a caller sends `text` + structured `filters`, and we
build the query ourselves. That means a user can never inject an OpenSearch/Lucene/SQL
expression (no-eval, like the authz condition evaluator). Cost limits refuse a query that
would be expensive to run rather than running it (wildcard/length DoS).
"""
from __future__ import annotations

from typing import Any


class QueryError(ValueError):
    pass


# We do not support wildcard/regex/fuzzy operators in the query STRING — typo tolerance is
# applied by the analyzer, not by user-supplied patterns. Reject these so nobody assumes them.
_FORBIDDEN = set("*?~^\\/")


def validate_text(text: str, *, max_len: int) -> str:
    text = str(text or "")
    if len(text) > max_len:
        raise QueryError("QUERY_TOO_LONG")
    if any(ch in _FORBIDDEN for ch in text):
        raise QueryError("QUERY_HAS_OPERATOR_CHARS")   # no wildcard/regex passthrough
    return text


def normalize_filters(filters: Any) -> dict[str, Any]:
    """Filters are field == value (or value ∈ list). No operators, no nesting → no injection."""
    if not filters:
        return {}
    if not isinstance(filters, dict):
        raise QueryError("FILTERS_MUST_BE_OBJECT")
    out: dict[str, Any] = {}
    for field, value in filters.items():
        if not isinstance(field, str) or not field:
            raise QueryError("BAD_FILTER_FIELD")
        if isinstance(value, (list, tuple)):
            out[field] = list(value)
        elif isinstance(value, (str, int, float, bool)):
            out[field] = value
        else:
            raise QueryError("BAD_FILTER_VALUE")
    return out


def check_terms(terms: list[str], *, max_terms: int) -> None:
    if len(terms) > max_terms:
        raise QueryError("TOO_MANY_TERMS")


def _eq(have: Any, want: Any) -> bool:
    """Type-aware equality (search-016). Python makes True == 1 and False == 0, so a `{"active": True}`
    filter would wrongly match a document whose field is the integer 1. When EITHER side is a bool, both
    must be bools to match; otherwise fall back to ordinary equality."""
    if isinstance(have, bool) or isinstance(want, bool):
        return type(have) is type(want) and have == want
    return have == want


def matches_filters(doc_fields: dict[str, Any], filters: dict[str, Any]) -> bool:
    for field, want in filters.items():
        have = doc_fields.get(field)
        if isinstance(want, list):
            if not any(_eq(have, w) for w in want):
                return False
        elif not _eq(have, want):
            return False
    return True
