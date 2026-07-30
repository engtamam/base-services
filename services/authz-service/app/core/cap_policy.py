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
            any(c.startswith(p) for p in denied_prefixes if p)
            or c in denied_caps
            or (bool(allowlist) and c not in allowlist)
        )
        if denied:
            seen.add(c)
            bad.append(c)
    return bad
