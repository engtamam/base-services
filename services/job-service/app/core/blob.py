"""Finding and removing a person inside an untyped blob.

Some services store arbitrary caller-supplied JSON: a webhook payload, a job's arguments, a synced
CRM record. There is no schema, so there is no column to erase — the person is somewhere inside a
dict nobody declared. This walks it.

Two matching rules, and the split between them is the whole safety argument:

  * **Exact value match, anywhere.** A string equal to the subject IS the subject. Precise, and it
    cannot over-match: "u_7" only redacts a field whose entire value is "u_7", never a field
    containing it by coincidence (a description mentioning "u_70", a URL, a hash).

  * **Containment, only under configured keys.** An email inside a rendered message body needs
    substring matching, but applying that everywhere would redact any field that happened to embed
    the subject's id — including other people's records that merely reference them. So containment
    is opt-in per key, named in config (ERASURE_TEXT_KEYS), never guessed from the data.

Redaction replaces the value with the subject_ref pseudonym rather than deleting the key. The
record's SHAPE survives — a payload with a missing field can crash a consumer that reads it on
replay, and an erasure must not turn into an outage. What leaves is the person, not the structure.
"""
from __future__ import annotations

from typing import Any

MAX_DEPTH = 32          # blobs are caller-supplied; a cyclic or absurdly nested one must not hang


def redact(value: Any, subject: str, replacement: str, text_keys: frozenset[str] = frozenset(),
           _key: str = "", _depth: int = 0) -> tuple[Any, int]:
    """Return `(redacted_value, hits)`. Pure: it never mutates the input.

    `hits` is what the ack reports, so it counts REPLACEMENTS, not rows — "we removed your address
    from 4 places in 2 payloads" is the honest statement.
    """
    if _depth > MAX_DEPTH or not subject:
        return value, 0

    if isinstance(value, str):
        if value == subject:
            return replacement, 1
        if _key in text_keys and subject in value:
            return value.replace(subject, replacement), 1
        return value, 0

    if isinstance(value, dict):
        out, hits = {}, 0
        for k, v in value.items():
            new, n = redact(v, subject, replacement, text_keys, _key=str(k), _depth=_depth + 1)
            out[k] = new
            hits += n
        return out, hits

    if isinstance(value, list):
        out_list, hits = [], 0
        for item in value:
            new, n = redact(item, subject, replacement, text_keys, _key=_key, _depth=_depth + 1)
            out_list.append(new)
            hits += n
        return out_list, hits

    return value, 0


def mentions(value: Any, subject: str, text_keys: frozenset[str] = frozenset()) -> bool:
    """Cheap check used to decide whether a row needs rewriting at all."""
    _, hits = redact(value, subject, "", text_keys)
    return hits > 0
