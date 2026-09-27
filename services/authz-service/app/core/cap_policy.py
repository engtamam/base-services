"""What a TENANT-scoped admin is allowed to hand out.

The escalation this closes: a role/assignment route accepts `capabilities` from the body and stores
them verbatim. A tenant owner holding `authz.manage` is forced to their OWN tenant by `_op_tenant`,
but nothing stopped them putting `platform.*` / `authz.manage` in a role and assigning it to
themselves — escalating out of their tenant. Tenant isolation on the ROW is not isolation on the
POWER the row grants.

The rule, applied ONLY to a tenant-scoped caller (a platform operator — `platform.authz` — is
unrestricted, because delegated administration is exactly their job):

  1. A hard DENYLIST is always refused — any capability under a denied prefix (`platform.`) or in the
     reserved set (`authz.manage`, …). This closes the actual hole even with no allowlist configured,
     so the service is safe by default, not only once someone remembers to fill a list.
  2. If a positive ALLOWLIST is configured, the caller may assign ONLY from it. Empty allowlist means
     "no positive restriction beyond the denylist" — deliberately, so turning this on does not brick
     every tenant that already manages benign, tenant-level roles.

Every input here is CONFIG (prefixes, reserved caps, allowlist) — nothing about which caps are
"platform" is hardcoded, because that vocabulary is the deployment's, not this function's.
"""
from __future__ import annotations

from collections.abc import Iterable


def _granter_holds(cap: str, granter_caps: frozenset[str], wildcard: bool) -> bool:
    """True if `granter_caps` already grants `cap` — the SAME semantics the RbacEngine decides with
    (app/adapters/policy/rbac.py:cap_matches), so a caller can grant exactly what its own token can
    already do and nothing more. `*` grants anything; `x.*` grants `x.<anything>`; otherwise an exact
    match. Kept here (not imported from the adapter) so this core rule has no upward dependency."""
    if cap in granter_caps or "*" in granter_caps:
        return True
    if wildcard:
        for held in granter_caps:
            if held.endswith(".*") and cap.startswith(held[:-1]):  # "files.*" -> "files."
                return True
    return False


def caps_exceeding_granter(
    caps: Iterable[str],
    granter_caps: frozenset[str],
    *,
    wildcard: bool = True,
) -> list[str]:
    """The subset gate: capabilities the granter does NOT itself hold, and therefore may not hand out.

    This is the caps<=granter rule (a caller can never grant a capability above its own power). It is
    DELIBERATELY independent of the platform.* denylist / allowlist in `forbidden_caps` — the denylist
    is a coarse, config-driven wall that can be misconfigured; this check needs no config at all and so
    cannot be silently disabled. The two are applied together at the user tier.

    Order-preserving and de-duplicated so a 403 can name exactly what was refused.
    """
    bad: list[str] = []
    seen: set[str] = set()
    for cap in caps:
        c = (cap or "").strip()
        if not c or c in seen:
            continue
        if not _granter_holds(c, granter_caps, wildcard):
            seen.add(c)
            bad.append(c)
    return bad


def forbidden_caps(
    caps: Iterable[str],
    *,
    is_operator: bool,
    denied_prefixes: tuple[str, ...],
    denied_caps: frozenset[str],
    allowlist: frozenset[str],
) -> list[str]:
    """The capabilities this caller may NOT assign. Empty list ⇒ the request is allowed.

    A platform operator is unrestricted (returns []). For a tenant-scoped caller, a capability is
    forbidden if it matches a denied prefix, is a reserved capability, or — when an allowlist is
    configured — is not in it. Order-preserving and de-duplicated so the 403 can name exactly what
    was rejected without repeating it.
    """
    if is_operator:
        return []
    bad: list[str] = []
    seen: set[str] = set()
    for cap in caps:
        c = (cap or "").strip()
        if not c or c in seen:
            continue
        denied = (
            c == "*"                                   # the god wildcard — cap_matches treats it as
                                                       # "holds everything", incl. platform.* (FIN authz-001)
            or _wildcard_reaches_denied(c, denied_prefixes)   # e.g. "platform.*" / "plat*" reach platform.
            or any(c.startswith(p) for p in denied_prefixes if p)
            or c in denied_caps
            or (bool(allowlist) and c not in allowlist)
        )
        if denied:
            seen.add(c)
            bad.append(c)
    return bad


def _wildcard_reaches_denied(cap: str, denied_prefixes: tuple[str, ...]) -> bool:
    """True when a PREFIX-wildcard capability (e.g. "platform.*", "plat*", "*") could grant something
    under a denied prefix. `cap_matches` honours `x.*` as "everything under x", so a tenant-scoped
    caller assigning "platform.*" (or a broader stem like "plat*") would mint exactly the platform
    power the denylist exists to forbid — the same class as the bare "*". Non-wildcard caps are handled
    by the plain startswith check."""
    if not cap.endswith("*"):
        return False
    stem = cap[:-1]                                     # "platform.*" -> "platform." ; "*" -> ""
    for p in denied_prefixes:
        if not p:
            continue
        # the wildcard's space overlaps the denied prefix's space in EITHER direction:
        #   cap "platform.*" (stem "platform.") ⊇/= denied "platform."  -> stem.startswith(p)
        #   cap "plat*"      (stem "plat")       ⊇ denied "platform."    -> p.startswith(stem)
        if stem.startswith(p) or p.startswith(stem):
            return True
    return False
