"""Whether a set of DIRECT capabilities may be granted onto a membership — as pure functions.

Capabilities are DERIVED from roles; authz-service is the authority. A membership row may ALSO carry
direct caps, and those are UNIONED into the minted token (caps_resolver). That union is a
privilege-escalation the moment the grant is ungoverned: a tenant-scoped admin could write a
capability onto a membership — their own second membership, say — that NO role in the tenant grants
and authz never approved, receive a token carrying it, and reach a capability-gated surface meant for
platform staff. A capability string is global; consumers guard on the string alone.

So the grant is governed at the ONE place a human's own authority is known — the write — and the
union can then only ever contain caps the granter already legitimately holds:

  subset     (default) — every direct cap must be within the GRANTER's own effective caps.
  off                  — no direct cap may be granted through a human route at all (roles-only).
  allowlist            — every direct cap must be within (granter caps ∩ the deployment allow-list).

Refused BY NAME, never silently dropped — a silent drop teaches the caller it worked. The internal
bootstrap (seed_owner, /internal/tenancy/members) passes NO granter and is exempt: the internal token
IS platform authority, and those doors are guarded separately (only_if_empty / roles-only, no caps).
"""
from __future__ import annotations

from collections.abc import Iterable

POLICIES = ("subset", "off", "allowlist")


class CapsNotGrantable(ValueError):
    """A direct-cap grant the membership write must refuse.

    Carries the offending cap BY NAME (it is a value the caller supplied, so echoing it leaks
    nothing) and the policy that refused it, so the API can answer 400 (feature off) vs 403 (not
    yours) and name what to remove.
    """

    def __init__(self, cap: str = "", *, policy: str = "subset") -> None:
        self.cap = cap
        self.policy = policy
        super().__init__(
            "direct caps are disabled (roles-only)" if policy == "off"
            else f"capability not grantable by this caller: {cap}")


def check(caps: Iterable[str] | None, *, granter_caps: Iterable[str] | None,
          policy: str = "subset", grantable: Iterable[str] = ()) -> None:
    """Raise CapsNotGrantable if any requested direct cap may not be granted by this granter.

    A no-op when there are no direct caps, or when `granter_caps` is None (the internal bootstrap,
    which is trusted and exempt). The FIRST offending cap is named.
    """
    want = [str(c) for c in (caps or [])]
    if not want or granter_caps is None:
        return
    pol = policy if policy in POLICIES else "subset"
    if pol == "off":
        raise CapsNotGrantable(want[0], policy="off")
    allowed = set(granter_caps)
    if pol == "allowlist":
        allowed &= set(grantable)
    for c in want:
        if c not in allowed:
            raise CapsNotGrantable(c, policy=pol)
