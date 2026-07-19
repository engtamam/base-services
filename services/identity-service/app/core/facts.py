"""The facts identity-service is willing to be asked about by another service.

onboarding declares requirements as `satisfied_by: "identity.email_verified"` — naming WHO OWNS
the truth. That only works if identity can actually be asked. It could not: the requirement was
declared, nothing served it, and those flows could never complete. The mirror image of a server
with no callers — a caller with no server.

A REGISTRY, not a route per fact. Every fact is one entry naming a resolver, so adding one is a
line here rather than an endpoint, and the set of things this service will answer is a list
somebody can read in one place. A fact that is not in this list is answered as UNKNOWN, never as
false — see below, because that distinction decides whether a stranger gets onboarded.

WHAT WE REFUSE TO ANSWER MATTERS AS MUCH AS WHAT WE ANSWER. `profile_complete` is declared by an
onboarding flow and is deliberately absent here: this service has no notion of a profile, and
inventing one — "has a username, so complete" — would answer a question nobody asked with a fact
nobody checked. An honest UNKNOWN leaves the requirement unsatisfied and visible; a confident
wrong answer completes onboarding for someone who never finished it.
"""
from __future__ import annotations

from typing import Any, Callable


class UnknownFact(KeyError):
    """This service does not answer that question. NOT the same as answering 'no'."""


async def _email_verified(*, users, memberships, tenant: str, subject: str,
                          legacy: str) -> bool | None:
    """Has this account proven its address? None when there is no such account.

    None is returned rather than False so the ROUTE can distinguish "no such user" (a 404, which
    the caller reads as unsatisfied — a person who does not exist has not verified anything) from
    "we cannot evaluate this", which must never read as a decision.
    """
    user = await users.get_by_username(tenant, subject)
    if user is None:
        return None
    if user.email_verified is True:
        return True
    if user.email_verified is None:
        # A row that predates verification. Consistent with the login gate: the same account must
        # not be allowed to log in and simultaneously reported as unverified to onboarding, or the
        # two halves of the platform disagree about the same person.
        return legacy != "enforce"
    return False


async def _has_members(*, users, memberships, tenant: str, subject: str,
                       legacy: str) -> bool | None:
    """Does this tenant have more than one member? Used by 'invite your team' style requirements."""
    if memberships is None:
        return None
    rows, _ = await memberships.members(tenant, limit=2)
    return len(rows) > 1


# name -> resolver. The whole contract, in one readable list.
FACTS: dict[str, Callable[..., Any]] = {
    "email_verified": _email_verified,
    "has_members": _has_members,
}


async def resolve(fact: str, **kw: Any) -> bool | None:
    if fact not in FACTS:
        raise UnknownFact(fact)
    return await FACTS[fact](**kw)
