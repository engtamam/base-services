"""Enforce that every MARKETING template is mapped to a consent purpose.

THE GAP THIS CLOSES. The consent gate in notifier.py only checks a send whose event or template_key
is present in NOTIFY_CONSENT_PURPOSES; anything unmapped is treated as transactional and sent with NO
consent check. So a genuinely marketing template a developer forgot to register in that map shipped
UNCHECKED, indistinguishable in the logs from a legitimate transactional send — a consent/compliance
hole that only configuration discipline stood between us and.

Now a template carries a `category` (the source of truth). A `marketing` template MUST have its key
in NOTIFY_CONSENT_PURPOSES, enforced two ways so it cannot be introduced by config OR at runtime:
  - BOOT: a seeded marketing template that is unmapped aborts startup (fail closed), rather than the
    old log-and-continue that would serve it unchecked.
  - WRITE: the admin upsert route refuses (400) a marketing template whose key is unmapped.
The gate then does the actual checking — this only guarantees a marketing template reaches it.
"""
from __future__ import annotations

from app.core.models import TEMPLATE_MARKETING


def is_unmapped_marketing(key: str, category: str, purposes: dict[str, str]) -> bool:
    """True when a template is marketing but its key is NOT mapped to a consent purpose — the exact
    misconfiguration that would let it send unchecked. Keyed by template key (the send path also
    accepts an event mapping, but a template's own key is the identity we can enforce at write time)."""
    return category == TEMPLATE_MARKETING and key not in (purposes or {})
