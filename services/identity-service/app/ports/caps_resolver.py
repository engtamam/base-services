"""CapsResolver — what a person may DO here, resolved at mint time.

The split this port exists to enforce:

    identity owns  "Sara is a compliance-manager at acme"   — a fact about employment.
    authz owns     "a compliance-manager may do X, Y, Z"    — policy.

Roles are the INPUT. Capabilities are DERIVED. Before this port, `caps` were typed onto the
membership row by hand at the moment someone was staffed, and `roles` was a parallel list of
strings that granted nothing — so editing the `compliance-manager` role changed nothing for
anybody who already held it. A role that means whatever it meant on the day you were hired is not
a role; it is a label.
"""
from __future__ import annotations

from typing import Protocol


class CapsResolver(Protocol):
    driver: str

    async def resolve(self, *, subject: str, tenant: str, roles: list[str],
                      caps: list[str]) -> list[str]:
        """The capabilities to put in the token.

        `caps` are the capabilities recorded directly on the membership (a direct grant, no role
        involved). `roles` are the roles held in THIS tenant. The result is what the token carries.

        Raises CapsUnavailable if the authority cannot be reached. It must NOT return an empty list
        in that case: an empty list is a valid answer meaning "this person may do nothing", and a
        token minted on a guess is a security decision made by a network timeout.
        """
        ...


class CapsUnavailable(Exception):
    """The capability authority could not be reached. FAIL CLOSED — no token.

    The tempting alternative is to mint the token with no caps and let the user in. Do not: a
    zero-cap token looks exactly like a permissions bug, and the way support "fixes" a permissions
    bug is by granting capabilities by hand — so an authz outage quietly turns into a permanent
    over-grant that nobody ever revokes. Refusing the login is loud, correct, and reversible.
    """
