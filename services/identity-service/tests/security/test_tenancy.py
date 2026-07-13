"""Phase 31 — the tenant wall. One test per threat in the phase-31 threat model.

Every test in this file is guarding the same sentence: **a token carries exactly one tenant, and
only ever a tenant the person is currently a member of.** Sixteen other services trust that claim
absolutely. If it is wrong, nothing downstream can tell.
"""
from __future__ import annotations

import asyncio
import dataclasses

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.login_identifier import UsernameIdentifier
from app.adapters.token_issuer.common import encode
from app.core.auth_service import AuthService
from app.core.login_attempts import LoginAttemptStore
from app.core.membership import (
    ACTIVE,
    SUSPENDED,
    Membership,
    MembershipStore,
    Person,
    PersonExists,
)
from app.core.refresh_store import RefreshStore
from app.core.tenancy import NoTenantClaim, TenantAccessDenied, tenant_of
from app.core.user_repository import UserRepository


def run(c):
    return asyncio.run(c)


class Hasher:
    driver = "plain"

    def hash(self, pw):
        return "h:" + pw

    def verify(self, pw, h):
        return h == "h:" + pw


class Issuer:
    """Records what was actually minted — the claims are the thing under test."""
    driver = "i"

    def __init__(self):
        self.minted: list[dict] = []

    def issue_access(self, sub, claims, ttl):
        self.minted.append({"sub": sub, **claims})
        return f"access.{sub}.{claims.get('tenant')}"

    def issue_refresh(self, sub, jti, ttl):
        return f"refresh.{sub}.{jti}"


class Verifier:
    driver = "v"

    def __init__(self):
        self.tokens: dict[str, dict] = {}

    def verify(self, token):
        return self.tokens[token]


def build(max_attempts=3):
    st = MemoryDataStore()
    ms = MembershipStore(st)
    issuer, verifier = Issuer(), Verifier()
    svc = AuthService(
        users=UserRepository(st, UsernameIdentifier()), refresh=RefreshStore(st),
        hasher=Hasher(), issuer=issuer, verifier=verifier, mfa=None,
        identifier=UsernameIdentifier(), access_ttl=900, refresh_ttl=3600,
        lockout=LoginAttemptStore(st, max_attempts=max_attempts, window_seconds=900,
                                  lockout_seconds=900),
        memberships=ms,
    )
    return svc, ms, issuer, verifier


def a_person(ms, ident="freelancer", pw="correct-horse"):
    return run(ms.create_person(Person(id="p1", identifier=ident, password_hash="h:" + pw)))


# ── T1 — mint a token for a tenant you are not a member of ───────────────────
def test_t1_cannot_mint_for_a_tenant_you_do_not_belong_to():
    svc, ms, issuer, _ = build()
    p = a_person(ms)
    run(ms.grant(Membership(person_id=p.id, tenant="own-shop", caps=["admin"])))

    with pytest.raises(TenantAccessDenied):
        run(svc.select_tenant(p, "victim-corp"))
    assert issuer.minted == [], "a refused mint must sign NOTHING"


def test_t1_tenant_in_the_token_comes_from_the_membership_row():
    """The request ASKS for a tenant. The token gets the tenant we FOUND. If a lookup ever
    returned a membership for a different tenant, the token must follow the row, not the ask."""
    svc, ms, issuer, _ = build()
    p = a_person(ms)
    run(ms.grant(Membership(person_id=p.id, tenant="bank", roles=["viewer"])))

    run(svc.select_tenant(p, "bank"))
    assert issuer.minted[-1]["tenant"] == "bank"


def test_t1_a_token_carries_exactly_one_tenant():
    svc, ms, issuer, _ = build()
    p = a_person(ms)
    run(ms.grant(Membership(person_id=p.id, tenant="own-shop")))
    run(ms.grant(Membership(person_id=p.id, tenant="bank")))

    run(svc.select_tenant(p, "own-shop"))
    run(svc.select_tenant(p, "bank"))

    for tok in issuer.minted:
        assert isinstance(tok["tenant"], str) and tok["tenant"]
        # No list, no set, no comma-joined smuggling of two tenants into one claim.
        assert "," not in tok["tenant"]
    assert [t["tenant"] for t in issuer.minted] == ["own-shop", "bank"]


# ── T3 — authority is per-door, never on the person ──────────────────────────
def test_t3_person_cannot_carry_caps_or_roles_at_all():
    """Not 'is empty' — DOES NOT EXIST. A field that exists is a field someone fills in."""
    names = {f.name for f in dataclasses.fields(Person)}
    assert "caps" not in names
    assert "roles" not in names


def test_t3_admin_at_my_own_shop_is_not_admin_at_the_bank():
    svc, ms, issuer, _ = build()
    p = a_person(ms)
    run(ms.grant(Membership(person_id=p.id, tenant="own-shop", caps=["admin", "billing:write"])))
    run(ms.grant(Membership(person_id=p.id, tenant="bank", caps=["reports:read"])))

    run(svc.select_tenant(p, "own-shop"))
    run(svc.select_tenant(p, "bank"))

    own, bank = issuer.minted
    assert own["caps"] == ["admin", "billing:write"]
    assert bank["caps"] == ["reports:read"], "the bank token must not inherit the shop's admin"
    assert "admin" not in bank["caps"]


# ── T4 — revocation bites at REFRESH, not at TTL ─────────────────────────────
def test_t4_refresh_fails_once_the_membership_is_revoked():
    svc, ms, issuer, verifier = build()
    p = a_person(ms)
    run(ms.grant(Membership(person_id=p.id, tenant="bank")))

    tokens = run(svc.select_tenant(p, "bank"))
    jti = tokens["refresh_token"].split(".")[-1]
    verifier.tokens[tokens["refresh_token"]] = {"type": "refresh", "sub": p.id, "jti": jti}

    # The contractor is removed from the bank. Their access token lives out its TTL — bounded and
    # accepted. Their refresh token must NOT quietly hand them a new one.
    run(ms.revoke(p.id, "bank"))

    assert run(svc.refresh(tokens["refresh_token"])) is None


def test_t4_refresh_fails_when_the_membership_is_suspended():
    svc, ms, _, verifier = build()
    p = a_person(ms)
    run(ms.grant(Membership(person_id=p.id, tenant="bank")))
    tokens = run(svc.select_tenant(p, "bank"))
    jti = tokens["refresh_token"].split(".")[-1]
    verifier.tokens[tokens["refresh_token"]] = {"type": "refresh", "sub": p.id, "jti": jti}

    run(ms.grant(Membership(person_id=p.id, tenant="bank", status=SUSPENDED)))
    assert run(svc.refresh(tokens["refresh_token"])) is None


def test_t4_refresh_still_works_while_the_membership_is_current():
    """The check must bite the revoked case and ONLY the revoked case — a guard that fails
    everything is not a guard, it is an outage."""
    svc, ms, issuer, verifier = build()
    p = a_person(ms)
    run(ms.grant(Membership(person_id=p.id, tenant="bank", caps=["reports:read"])))
    tokens = run(svc.select_tenant(p, "bank"))
    jti = tokens["refresh_token"].split(".")[-1]
    verifier.tokens[tokens["refresh_token"]] = {"type": "refresh", "sub": p.id, "jti": jti}

    fresh = run(svc.refresh(tokens["refresh_token"]))
    assert fresh is not None
    assert issuer.minted[-1]["tenant"] == "bank"
    assert issuer.minted[-1]["caps"] == ["reports:read"]


def test_t4_refresh_carries_its_tenant_and_re_mints_only_that_one():
    """Two live sessions, two tenants. Refreshing the bank session must not yield a shop token."""
    svc, ms, issuer, verifier = build()
    p = a_person(ms)
    run(ms.grant(Membership(person_id=p.id, tenant="own-shop", caps=["admin"])))
    run(ms.grant(Membership(person_id=p.id, tenant="bank", caps=["reports:read"])))

    bank = run(svc.select_tenant(p, "bank"))
    jti = bank["refresh_token"].split(".")[-1]
    verifier.tokens[bank["refresh_token"]] = {"type": "refresh", "sub": p.id, "jti": jti}

    run(svc.refresh(bank["refresh_token"]))
    assert issuer.minted[-1]["tenant"] == "bank"
    assert issuer.minted[-1]["caps"] == ["reports:read"]


def test_t4_refresh_fails_closed_when_the_membership_store_is_gone():
    """A tenant-scoped refresh row with no store to check it against must fail CLOSED. Turning
    IDENTITY_MULTI_TENANT off must not silently un-revoke every fired contractor."""
    svc, ms, _, verifier = build()
    p = a_person(ms)
    run(ms.grant(Membership(person_id=p.id, tenant="bank")))
    tokens = run(svc.select_tenant(p, "bank"))
    jti = tokens["refresh_token"].split(".")[-1]
    verifier.tokens[tokens["refresh_token"]] = {"type": "refresh", "sub": p.id, "jti": jti}

    svc._memberships = None                      # the knob went off under a live session
    assert run(svc.refresh(tokens["refresh_token"])) is None


# ── T5 — lockout must not become a cross-tenant denial of service ────────────
def test_t5_lockout_from_one_source_does_not_disarm_the_person_everywhere():
    """Brute-forcing consultant@x.com must not remove them from all five of their clients.
    The counter is keyed (person, source), so another source still authenticates."""
    svc, ms, _, _ = build(max_attempts=3)
    p = a_person(ms, pw="correct-horse")
    run(ms.grant(Membership(person_id=p.id, tenant="bank")))

    for _ in range(4):
        assert run(svc.authenticate_person("freelancer", "wrong", "1.2.3.4")) is None

    assert run(svc.authenticate_person("freelancer", "correct-horse", "1.2.3.4")) is None, \
        "the attacking source IS locked out"
    got = run(svc.authenticate_person("freelancer", "correct-horse", "9.9.9.9"))
    assert got is not None and got.id == p.id, \
        "the real consultant, elsewhere, must still be able to work"


def test_t5_a_good_login_clears_the_counter():
    svc, ms, _, _ = build(max_attempts=3)
    a_person(ms, pw="correct-horse")
    run(svc.authenticate_person("freelancer", "wrong", "1.2.3.4"))
    run(svc.authenticate_person("freelancer", "wrong", "1.2.3.4"))
    assert run(svc.authenticate_person("freelancer", "correct-horse", "1.2.3.4")) is not None
    for _ in range(2):
        run(svc.authenticate_person("freelancer", "wrong", "1.2.3.4"))
    assert run(svc.authenticate_person("freelancer", "correct-horse", "1.2.3.4")) is not None


# ── T6 — no enumeration: "no such tenant" == "not your tenant" ───────────────
def test_t6_unknown_tenant_and_forbidden_tenant_are_indistinguishable():
    svc, ms, _, _ = build()
    p = a_person(ms)
    run(ms.grant(Membership(person_id=p.id, tenant="own-shop")))

    errs = []
    for tenant in ("does-not-exist-at-all", "real-competitor-corp"):
        with pytest.raises(TenantAccessDenied) as e:
            run(svc.select_tenant(p, tenant))
        errs.append(str(e.value))

    assert errs[0] == errs[1], "the two answers must be byte-identical or they leak the client list"
    assert errs[0] == "", "and they must say nothing at all"


# ── T7 — two humans, one email: REFUSE, never merge ──────────────────────────
def test_t7_a_colliding_identifier_is_refused_not_merged():
    """alice@x.com at the law firm and alice@x.com at the bakery may be two different humans.
    Merging them hands one of them the other's employer. A refusal is an operator's afternoon."""
    _, ms, _, _ = build()
    run(ms.create_person(Person(id="p1", identifier="alice", password_hash="h:one")))

    with pytest.raises(PersonExists):
        run(ms.create_person(Person(id="p2", identifier="alice", password_hash="h:two")))

    survivor = run(ms.person_by_identifier("alice"))
    assert survivor.id == "p1", "and the refusal must not have half-written the second person"
    assert run(ms.person("p2")) is None


# ── T8 — a token with no tenant claim is rejected, never widened ─────────────
def test_t8_missing_tenant_claim_is_rejected_not_a_wildcard():
    for claims in ({}, {"tenant": ""}, {"tenant": None}, {"caps": ["admin"]}):
        with pytest.raises(NoTenantClaim):
            tenant_of(claims)


def test_t8_tenant_of_returns_the_claim_when_present():
    assert tenant_of({"tenant": "bank"}) == "bank"


# ── T9 — nothing to stand in means no token at all ───────────────────────────
def test_t9_zero_memberships_gets_no_token():
    svc, ms, issuer, _ = build()
    p = a_person(ms)                              # a real person, hired by nobody
    with pytest.raises(TenantAccessDenied):
        run(svc.select_tenant(p, "anywhere"))
    assert issuer.minted == []


def test_t9_empty_tenant_is_not_all_tenants():
    svc, ms, issuer, _ = build()
    p = a_person(ms)
    run(ms.grant(Membership(person_id=p.id, tenant="bank")))
    with pytest.raises(TenantAccessDenied):
        run(svc.select_tenant(p, ""))
    assert issuer.minted == []


def test_t9_a_suspended_person_gets_no_token_anywhere():
    svc, ms, issuer, _ = build()
    p = a_person(ms)
    run(ms.grant(Membership(person_id=p.id, tenant="bank")))
    p = dataclasses.replace(p, status=SUSPENDED)
    with pytest.raises(TenantAccessDenied):
        run(svc.select_tenant(p, "bank"))
    assert issuer.minted == []


def test_t9_a_suspended_membership_gets_no_token_for_that_tenant_only():
    svc, ms, issuer, _ = build()
    p = a_person(ms)
    run(ms.grant(Membership(person_id=p.id, tenant="bank", status=SUSPENDED)))
    run(ms.grant(Membership(person_id=p.id, tenant="own-shop", status=ACTIVE)))

    with pytest.raises(TenantAccessDenied):
        run(svc.select_tenant(p, "bank"))
    assert run(svc.select_tenant(p, "own-shop"))["access_token"]
    assert [t["tenant"] for t in issuer.minted] == ["own-shop"]


# ── the signer owns its claims ───────────────────────────────────────────────
def test_extra_claims_cannot_overwrite_sub_or_type():
    """`payload.update(extra)` would let a caller rewrite who the token is for. The mint path is
    not the place to leave that lying around on the argument that nobody has used it yet."""
    for bad in ({"sub": "someone-else"}, {"type": "access"}, {"exp": 1}):
        with pytest.raises(ValueError):
            encode(key="k" * 40, algorithm="HS256", subject="p1", issuer="i", audience="a",
                   ttl_seconds=60, token_type="refresh", extra=bad)


def test_ordinary_claims_still_sign():
    tok = encode(key="k" * 40, algorithm="HS256", subject="p1", issuer="i", audience="a",
                 ttl_seconds=60, token_type="access",
                 extra={"tenant": "bank", "caps": [], "roles": []})
    assert tok.count(".") == 2
