"""Roles are the INPUT. Capabilities are DERIVED.

Before this, `caps` were typed onto the membership row by hand when someone was staffed, and
`roles` was a parallel list of strings that granted nothing. Editing the `compliance-manager` role
changed nothing for anybody already holding it — which means it was not a role, it was a label.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.caps_resolver.membership import MembershipCaps
from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.login_identifier import UsernameIdentifier
from app.core.auth_service import AuthService
from app.core.membership import Membership, MembershipStore, Person
from app.core.refresh_store import RefreshStore
from app.core.user_repository import UserRepository
from app.ports.caps_resolver import CapsUnavailable


def run(c):
    return asyncio.run(c)


class Hasher:
    driver = "plain"

    def hash(self, pw):
        return "h:" + pw

    def verify(self, pw, h):
        return h == "h:" + pw


class Issuer:
    driver = "i"

    def __init__(self):
        self.minted = []

    def issue_access(self, sub, claims, ttl):
        self.minted.append({"sub": sub, **claims})
        return f"access.{sub}"

    def issue_refresh(self, sub, jti, ttl):
        return f"refresh.{sub}.{jti}"


class FakeAuthz:
    """Stands in for authz-service: a role -> capabilities map, per tenant."""
    driver = "authz"

    def __init__(self, roles: dict, fail: bool = False):
        self._roles = roles          # {(tenant, role): [caps]}
        self.fail = fail
        self.calls = 0

    async def resolve(self, *, subject, tenant, roles, caps):
        self.calls += 1
        if self.fail:
            raise CapsUnavailable("authz-service unreachable")
        derived = set(caps)
        for r in roles:
            derived |= set(self._roles.get((tenant, r), []))
        return sorted(derived)


def build(resolver):
    st = MemoryDataStore()
    ms = MembershipStore(st)
    issuer = Issuer()
    svc = AuthService(
        users=UserRepository(st, UsernameIdentifier()), refresh=RefreshStore(st),
        hasher=Hasher(), issuer=issuer, verifier=None, mfa=None,
        identifier=UsernameIdentifier(), access_ttl=900, refresh_ttl=3600,
        memberships=ms, caps_resolver=resolver)
    return svc, ms, issuer


def a_person(ms):
    return run(ms.create_person(Person(id="sara", identifier="sara",
                                       password_hash="h:correct-horse")))


# ── a role is a role ─────────────────────────────────────────────────────────
def test_a_role_grants_the_capabilities_authz_says_it_grants():
    """Sara is staffed as compliance-manager with NO caps typed in by hand."""
    authz = FakeAuthz({("acme", "compliance-manager"): ["dsar:read", "dsar:close"]})
    svc, ms, issuer = build(authz)
    p = a_person(ms)
    run(ms.grant(Membership(person_id=p.id, tenant="acme", roles=["compliance-manager"])))

    run(svc.select_tenant(p, "acme"))
    assert issuer.minted[-1]["caps"] == ["dsar:close", "dsar:read"]
    assert issuer.minted[-1]["roles"] == ["compliance-manager"]


def test_editing_the_role_changes_what_everyone_holding_it_may_do():
    """The whole point. The admin is the control plane; nothing is typed in by hand."""
    authz = FakeAuthz({("acme", "compliance-manager"): ["dsar:read"]})
    svc, ms, issuer = build(authz)
    p = a_person(ms)
    run(ms.grant(Membership(person_id=p.id, tenant="acme", roles=["compliance-manager"])))

    run(svc.select_tenant(p, "acme"))
    assert issuer.minted[-1]["caps"] == ["dsar:read"]

    # An admin edits the ROLE in authz. Sara's membership row is not touched.
    authz._roles[("acme", "compliance-manager")] = ["dsar:read", "dsar:close"]

    run(svc.select_tenant(p, "acme"))
    assert issuer.minted[-1]["caps"] == ["dsar:close", "dsar:read"], \
        "her next mint must reflect the role as it is NOW, not as it was when she was hired"


def test_a_direct_grant_still_works_alongside_a_role():
    authz = FakeAuthz({("acme", "auditor"): ["audit:read"]})
    svc, ms, issuer = build(authz)
    p = a_person(ms)
    run(ms.grant(Membership(person_id=p.id, tenant="acme",
                            roles=["auditor"], caps=["reports:export"])))

    run(svc.select_tenant(p, "acme"))
    assert issuer.minted[-1]["caps"] == ["audit:read", "reports:export"]


def test_a_role_in_another_tenant_grants_nothing_here():
    """T3, through the role door: admin at her own shop is not admin at the bank."""
    authz = FakeAuthz({("own-shop", "admin"): ["everything:*"],
                       ("bank", "contractor"): ["reports:read"]})
    svc, ms, issuer = build(authz)
    p = a_person(ms)
    run(ms.grant(Membership(person_id=p.id, tenant="own-shop", roles=["admin"])))
    # Same role NAME at the bank — and the bank has no such role.
    run(ms.grant(Membership(person_id=p.id, tenant="bank", roles=["admin"])))

    run(svc.select_tenant(p, "bank"))
    assert issuer.minted[-1]["caps"] == [], \
        "a role id from another company must expand to NOTHING here"


# ── fail closed ──────────────────────────────────────────────────────────────
def test_an_unreachable_authz_mints_no_token_at_all():
    """NOT a token with zero caps. A zero-cap token looks exactly like a permissions bug, and the
    way support fixes a permissions bug is by granting caps by hand — so an outage would quietly
    become a permanent over-grant that nobody ever revokes."""
    authz = FakeAuthz({}, fail=True)
    svc, ms, issuer = build(authz)
    p = a_person(ms)
    run(ms.grant(Membership(person_id=p.id, tenant="acme", roles=["compliance-manager"])))

    with pytest.raises(CapsUnavailable):
        run(svc.select_tenant(p, "acme"))
    assert issuer.minted == [], "a refused mint must sign NOTHING"


def test_membership_driver_needs_no_authz_and_keeps_todays_behaviour():
    svc, ms, issuer = build(MembershipCaps())
    p = a_person(ms)
    run(ms.grant(Membership(person_id=p.id, tenant="acme",
                            roles=["compliance-manager"], caps=["dsar:read"])))

    run(svc.select_tenant(p, "acme"))
    # The role is carried, but it grants nothing — the documented limitation of this driver.
    assert issuer.minted[-1]["caps"] == ["dsar:read"]
    assert issuer.minted[-1]["roles"] == ["compliance-manager"]


# ── identity-001: assigning a ROLE is governed like assigning its caps ──────────────────────────────
def test_a_members_write_admin_cannot_assign_a_role_that_exceeds_its_caps():
    from app.core.caps_grant import CapsNotGrantable
    # authz maps 'owner' -> platform.admin; the granting admin holds only members:write + billing.read.
    resolver = FakeAuthz({("acme", "owner"): ["platform.admin"],
                          ("acme", "support"): ["billing.read"]})
    svc, ms, _ = build(resolver)
    granter = frozenset({"members:write", "billing.read"})
    # Assigning 'owner' derives platform.admin, which the granter does not hold → refused.
    with pytest.raises(CapsNotGrantable):
        run(svc.add_member("acme", "victim", roles=["owner"], granter_caps=granter))
    # Assigning 'support' derives only billing.read, which the granter holds → allowed.
    m, _ = run(svc.add_member("acme", "agent", roles=["support"], granter_caps=granter,
                       mode="password", password="correct-horse-battery"))
    assert m is not None


def test_internal_bootstrap_may_assign_any_role():
    # granter_caps=None is the internal-bootstrap exemption (seed_owner / internal add-member).
    resolver = FakeAuthz({("acme", "owner"): ["platform.admin"]})
    svc, ms, _ = build(resolver)
    m, _ = run(svc.add_member("acme", "founder", roles=["owner"], granter_caps=None,
                     mode="password", password="correct-horse-battery"))
    assert m is not None
