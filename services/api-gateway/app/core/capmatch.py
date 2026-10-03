"""Capability matching — the ONE source of truth for how a caller's held caps satisfy a requirement.

Both the admin/app catalogs (what to SHOW) and the gateway's request enforcement (what to ALLOW)
must decide "does this caller hold what's needed?" the SAME way, or the catalog promises an action
the gateway then 403s (a real, reported inconsistency). This module is that shared decision so the
two can never drift.

Semantics:
- WILDCARD grants: a held `*` satisfies anything; a held `a.b.*` satisfies `a.b.<anything>`.
- A route/action listing MULTIPLE capabilities means ANY-OF (OR): holding one of them is enough —
  "any of these roles may use this", not "must hold all simultaneously". (The old gateway check used
  set.issubset, i.e. AND, which denied a caller who held a broader/alternative cap.)
"""
from __future__ import annotations

from typing import Iterable


def holds(held: Iterable[str], needed: str) -> bool:
    """Does the caller hold `needed`? Honours wildcard grants like `platform.*` and `*`."""
    held = held if isinstance(held, (set, frozenset)) else set(held)
    if needed in held or "*" in held:
        return True
    for h in held:                       # a held `a.b.*` grants `a.b.anything`
        # Require a NAMESPACE boundary (api-gateway-014): only `a.b.*` (prefix ending in '.') is a
        # wildcard grant. A bare `*` is the sole full grant (handled above); a non-dot form like
        # `plat*` is NOT honoured, so it cannot over-grant `platform.secrets`.
        if h.endswith(".*") and needed.startswith(h[:-1]):
            return True
    return False


def holds_any(held: Iterable[str], needed: Iterable[str]) -> bool:
    """ANY-OF: the caller holds at least one of the needed capabilities (each wildcard-aware).

    An EMPTY `needed` means nothing is required → True (the route/action gates on auth alone)."""
    held = held if isinstance(held, (set, frozenset)) else set(held)
    needed = list(needed)
    if not needed:
        return True
    return any(holds(held, n) for n in needed)
