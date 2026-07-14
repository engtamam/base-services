"""THE MINT GUARD — the only door between every company on this platform.

Sixteen services scope everything to the `tenant` claim in the access token, and they are RIGHT to
trust it: that trust is what makes the architecture work. Which means the moment a token is minted
is the only moment the tenant is ever checked. Everywhere else it is simply believed.

If the check below is missing, weak, or bypassable, it is not a bug in identity-service. It is a
total cross-tenant breach of the entire platform — and every other gate in this repository stays
green while a competitor reads your invoices.

So this file is small on purpose, and every line of it is load-bearing.

    T1 (CWE-639/863) the tenant in the token comes from the MEMBERSHIP ROW WE FOUND,
                     never from the request. A request may ASK for a tenant; it may never
                     SUPPLY one.
    T3 (CWE-269)     caps and roles come from the Membership. The Person carries none — the
                     field does not exist. A freelancer who is admin at their own company is
                     not admin at the bank.
    T6 (CWE-204/208) "no such tenant" and "not your tenant" are the SAME answer. Otherwise the
                     login page tells a competitor who your consultants work for.
    T8               a token with no tenant claim is REJECTED. "No tenant" must never widen
                     into "all tenants" — fail closed, never open.
    T9               zero memberships, a suspended membership, or a suspended person yields NO
                     token at all. Not an empty one. Not a "global" one.
"""
from __future__ import annotations

from typing import Any

from app.core.membership import ACTIVE, Membership, Person


class TenantAccessDenied(Exception):
    """Uniform. Deliberately says nothing about WHY (T6).

    A caller learns only "no". They never learn whether the tenant exists, whether they were
    removed, or whether they were never a member — because each of those is a fact about someone
    else's company.
    """


class NoTenantClaim(Exception):
    """A token arrived without a tenant. It is rejected, not treated as a wildcard (T8)."""


def claims_for(person: Person, membership: Membership,
               caps: list[str] | None = None) -> dict[str, Any]:
    """The ONLY function that may put a tenant into a token.

    Note what it does not accept: a requested tenant. There is no parameter for one. The tenant is
    read off the membership we verified, so a request cannot smuggle one past the check even if a
    future caller forgets to validate — the mistake is unrepresentable rather than merely
    discouraged.

    `caps` are the RESOLVED capabilities (see the CapsResolver port). They still describe authority
    in exactly one tenant — the resolver is asked about this membership's tenant and no other — so
    T3 holds regardless of where the caps came from. When no resolver is wired the membership's own
    caps are used, which is the behaviour identity has always had.
    """
    if not membership.active:
        raise TenantAccessDenied()
    if membership.person_id != person.id:
        # Belt and braces: a membership fetched for the wrong person must never mint a token.
        raise TenantAccessDenied()
    return {
        "tenant": membership.tenant,      # <- from the ROW, never from the request
        "caps": list(membership.caps if caps is None else caps),   # authority is per-door (T3)
        "roles": list(membership.roles),  # the INPUT that derived the caps — never authority itself
    }


async def authorize_tenant(store, person: Person, requested_tenant: str) -> Membership:
    """Verify the person may stand in this room, and return the key we found.

    Called on EVERY mint: login, tenant switch, and every refresh. O(1) keyed lookup.

    Every failure raises the SAME exception with the SAME message (T6). A caller cannot tell
    "that company does not exist" from "you were fired last week" — both are facts about a
    company that is not theirs.
    """
    if person.status != ACTIVE:
        raise TenantAccessDenied()
    if not requested_tenant:
        # T8/T9: an empty tenant is not "any tenant". It is no tenant, and no tenant gets no token.
        raise TenantAccessDenied()

    membership = await store.membership(person.id, requested_tenant)
    if membership is None or not membership.active:
        raise TenantAccessDenied()
    return membership


def tenant_of(claims: dict[str, Any]) -> str:
    """Read the tenant OUT of a verified token, failing closed if it is absent (T8).

    `claims.get("tenant") or "global"` is the bug this exists to prevent: a token that lost its
    tenant would silently become a token for everything.
    """
    tenant = (claims or {}).get("tenant") or ""
    if not tenant:
        raise NoTenantClaim("token carries no tenant claim — rejected, not widened")
    return tenant
