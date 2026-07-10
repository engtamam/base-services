"""Deterministic percentage rollout — sticky without storing per-user state.

bucket = sha256(salt : flag_key : subject) mod 10_000  → a stable 0.00–99.99% slot.
The same subject always lands in the same bucket (no flicker across restarts/replicas),
and the flag key is part of the digest so two 50% flags don't hit the same half of users.
"""
from __future__ import annotations

import hashlib

BUCKETS = 10_000


def bucket(salt: str, key: str, subject: str) -> int:
    digest = hashlib.sha256(f"{salt}:{key}:{subject}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % BUCKETS


def in_rollout(salt: str, key: str, subject: str, pct: float) -> bool:
    """pct is 0..100. 0 → nobody, 100 → everybody. No subject → not in rollout (fail-safe)."""
    if pct >= 100:
        return True
    if pct <= 0 or not subject:
        return False
    return bucket(salt, key, subject) < int(pct * BUCKETS / 100)


def subject_for(bucket_by: str, ctx: dict) -> str:
    """WHAT the rollout hashes. Per-tenant keeps a whole company consistent (schema
    rollouts); per-user is right for UI tests. A missing subject → "" → not in rollout."""
    if bucket_by == "tenant":
        return str(ctx.get("tenant") or "")
    if bucket_by.startswith("attr:"):
        return str((ctx.get("attrs") or {}).get(bucket_by[5:], "") or "")
    return str(ctx.get("user") or "")


def validate_variants(variants: dict[str, float]) -> None:
    if not variants:
        return
    total = sum(float(w) for w in variants.values())
    if any(float(w) < 0 for w in variants.values()) or abs(total - 100.0) > 1e-6:
        raise ValueError(f"variant weights must be non-negative and sum to 100 (got {total})")


def pick_variant(salt: str, key: str, subject: str, variants: dict[str, float]) -> str | None:
    """Deterministic weighted assignment from the SAME bucket — sticky, no storage."""
    if not variants or not subject:
        return None
    b = bucket(salt, key, subject)
    upper = 0
    for name, weight in variants.items():          # declared order defines the bands
        upper += int(float(weight) * BUCKETS / 100)
        if b < upper:
            return name
    return next(reversed(variants))                # rounding remainder → last band
