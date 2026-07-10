"""The evaluation engine (pure) — precedence chain, first match wins, always with a REASON.

   1 unknown flag         → off            unknown_flag
   2 archived / killed    → off            kill_switch
   3 outside schedule     → default        scheduled
   4 prerequisite not met → off            prerequisite_failed
   5 entitlement denied   → off            entitlement_denied
   6 user override        → override       user_override
   7 tenant override      → override       tenant_override
   8 segment match        → on             segment_match
   9 targeting rule       → rule value     rule_match
  10 weighted variants    → variant        variant_rollout
  11 rollout bucket       → on             rollout
  12 otherwise            → flag default   default

Reasons make flags debuggable: "why was it on for that user?" is always answerable.
Pure: prerequisites/segments/entitlement are RESOLVED by the caller and passed in.
"""
from __future__ import annotations

from typing import Any

from app.core import rollout as _rollout
from app.core import rules as _rules


def decision(enabled: bool, reason: str, variant: str = "") -> dict[str, Any]:
    return {"enabled": bool(enabled), "reason": reason, "variant": variant}


def in_schedule(flag: dict, now: float) -> bool:
    start, end = float(flag.get("start", 0) or 0), float(flag.get("end", 0) or 0)
    if start and now < start:
        return False
    if end and now >= end:
        return False
    return True


def evaluate(flag: dict[str, Any] | None, ctx: dict[str, Any], *, salt: str,
             entitled: bool | None = None, now: float = 0.0,
             prereqs_ok: bool = True, in_segment: bool = False) -> dict[str, Any]:
    """ctx: {tenant, user, attrs{...}}."""
    if not flag:
        return decision(False, "unknown_flag")                       # fail-safe

    key = flag.get("key", "")
    if flag.get("archived") or not flag.get("enabled", False):
        return decision(False, "kill_switch")                        # master switch beats all

    if not in_schedule(flag, now):
        return decision(bool(flag.get("default", False)), "scheduled")

    if flag.get("prerequisites") and not prereqs_ok:
        return decision(False, "prerequisite_failed")

    if flag.get("requires_entitlement") and entitled is False:
        return decision(False, "entitlement_denied")

    user = str(ctx.get("user") or "")
    tenant = str(ctx.get("tenant") or "")
    attrs = ctx.get("attrs") or {}

    user_ov = flag.get("user_overrides") or {}
    if user and user in user_ov:
        return decision(bool(user_ov[user]), "user_override")

    tenant_ov = flag.get("tenant_overrides") or {}
    if tenant and tenant in tenant_ov:
        return decision(bool(tenant_ov[tenant]), "tenant_override")

    if flag.get("segments") and in_segment:
        return decision(True, "segment_match")

    rule = _rules.first_match(flag.get("rules") or [], attrs)
    if rule is not None:
        return decision(bool(rule.get("enabled", True)), "rule_match", str(rule.get("variant", "")))

    # what the rollout hashes: per-user (UI tests) vs per-tenant (schema rollouts)
    subject = _rollout.subject_for(str(flag.get("bucket_by") or "user"), ctx)
    pct = float(flag.get("rollout_pct", 100) or 0)

    if not _rollout.in_rollout(salt, key, subject, pct):
        return decision(bool(flag.get("default", False)), "default")

    variants = flag.get("variants") or {}
    if variants:
        chosen = _rollout.pick_variant(salt, key, subject, variants)
        if chosen is not None:
            # a variant named "off"/"control" that maps to disabled is the caller's choice;
            # `enabled` stays True — the variant carries the meaning.
            return decision(True, "variant_rollout", chosen)

    return decision(True, "rollout")
