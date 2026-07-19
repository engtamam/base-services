"""Self-registration must not be able to grant itself anything.

THE BUG, reported externally and reproduced before it was fixed:

    POST /api/v1/auth/register is unauthenticated (rate limit + REGISTRATION_ENABLED, which
    DEFAULTS TO TRUE). It passed body.caps and body.roles straight into the stored User, and
    issue_tokens copies user.caps into the access token. So:

        {"username": "...", "password": "...", "tenant": "acme", "caps": ["*"]}

    produced a token whose caps contained "*" — and api-gateway's _caps_allow honours "*" as a
    wildcard matching EVERY capability check on the platform. One unauthenticated POST, total
    compromise, and the caller chose the tenant too.

core/tenancy.py — the mint guard — already stated the rule for the multi-tenant path: "a request
may ASK for a tenant; it may never SUPPLY one", and "caps and roles come from the Membership; the
Person carries none". The legacy single-tenant register route bypassed that guard entirely. The
door was built and the side entrance was left open.

Two independent defences, because one of them is a route and routes get added:

  1. the request model has no caps/roles fields at all — privilege comes from config
  2. the MINT strips platform.* and "*" from any token whose tenant is not the platform tenant
"""
from __future__ import annotations

import asyncio

import pytest

from app.api.v1.auth import RegisterIn
from app.core.auth_service import AuthService


def run(c):
    return asyncio.run(c)


class _Users:
    async def get_by_username(self, t, u):
        return None

    async def create(self, u):
        return u


class _Hasher:
    def hash(self, p):
        return "h:" + p


class _Ident:
    driver = "email"

    def validate(self, v):
        return True

    def display(self, v):
        return v.strip().casefold()


class _Issuer:
    def __init__(self):
        self.claims = None

    def issue_access(self, sub, claims, ttl):
        self.claims = claims
        return "ACCESS"

    def issue_refresh(self, sub, jti, ttl):
        return "REFRESH"


class _Refresh:
    async def issue(self, jti, uid, exp):
        return None


def _svc(platform_tenant: str = "platform"):
    iss = _Issuer()
    s = AuthService.__new__(AuthService)
    s._users, s._hasher, s._identifier, s._min_pw = _Users(), _Hasher(), _Ident(), 1
    s._issuer, s._refresh = iss, _Refresh()
    s._access_ttl, s._refresh_ttl = 900, 3600
    s._platform_tenant = platform_tenant
    return s, iss


# ── defence 1: the request cannot even express a capability ────────────────────
def test_the_register_body_has_NO_caps_or_roles_fields():
    """THE fix. Not validated — absent. A field that does not exist cannot be forgotten about in
    a later refactor the way a validation branch can."""
    assert "caps" not in RegisterIn.model_fields
    assert "roles" not in RegisterIn.model_fields


def test_a_client_still_SENDING_caps_is_accepted_and_the_caps_discarded():
    """Silently ignored rather than rejected, deliberately. A 400 naming the field would tell an
    attacker exactly what to rename, and would break existing clients for no security gain."""
    body = RegisterIn(username="ada@example.com", password="x" * 12, tenant="acme",
                      caps=["*", "platform.admin"], roles=["superadmin"])
    assert not hasattr(body, "caps")
    assert not hasattr(body, "roles")


# ── defence 2: the mint refuses to put platform authority on a tenant token ────
def test_platform_caps_are_STRIPPED_from_a_non_platform_tenant_token():
    """The escalation, reproduced end to end. Before the fix these landed in the token."""
    svc, iss = _svc(platform_tenant="platform")
    user = run(svc.register("acme", "attacker@evil.com", "password123",
                            ["*", "platform.admin", "platform.tenants", "billing.self"], []))
    run(svc.issue_tokens(user))
    assert iss.claims["caps"] == ["billing.self"]


def test_the_bare_wildcard_is_stripped_too():
    """`*` matters more than platform.admin and is easier to miss: the gateway honours it as a
    wildcard over EVERY capability check, and it contains none of the letters anyone greps for."""
    svc, iss = _svc()
    user = run(svc.register("acme", "a@b.com", "password123", ["*"], []))
    run(svc.issue_tokens(user))
    assert iss.claims["caps"] == []


def test_a_prefix_wildcard_does_not_sneak_through():
    """`platform.*` is honoured by the gateway's prefix matching, so it must go the same way."""
    svc, iss = _svc()
    user = run(svc.register("acme", "a@b.com", "password123", ["platform.*"], []))
    run(svc.issue_tokens(user))
    assert iss.claims["caps"] == []


def test_the_REAL_platform_operator_keeps_their_capabilities():
    """The check must not be a blanket ban, or it locks an operator out of their own platform."""
    svc, iss = _svc(platform_tenant="platform")
    user = run(svc.register("platform", "ops@corp.com", "password123",
                            ["platform.admin", "platform.tenants"], []))
    run(svc.issue_tokens(user))
    assert iss.claims["caps"] == ["platform.admin", "platform.tenants"]


def test_ordinary_capabilities_are_untouched():
    svc, iss = _svc()
    user = run(svc.register("acme", "a@b.com", "password123",
                            ["billing.self", "stream.view"], []))
    run(svc.issue_tokens(user))
    assert iss.claims["caps"] == ["billing.self", "stream.view"]


def test_an_empty_PLATFORM_TENANT_preserves_the_previous_behaviour():
    """An upgrade must not lock an operator out of a platform that never named its tenant. The
    setting being empty is a decision, and it is logged at boot rather than left silent."""
    svc, iss = _svc(platform_tenant="")
    user = run(svc.register("acme", "a@b.com", "password123", ["platform.admin"], []))
    run(svc.issue_tokens(user))
    assert iss.claims["caps"] == ["platform.admin"]


# ── the config that replaced the request body ─────────────────────────────────
def test_self_registration_grants_NOTHING_by_default():
    """A stranger who signs themself up is nobody until somebody grants them something."""
    from app.config import Settings

    s = Settings(INTERNAL_SERVICE_TOKEN="x" * 40)
    assert s.registration_default_roles == []


def test_default_roles_are_ROLES_not_capabilities():
    """Config names roles, which a resolver maps to capabilities. Letting config name raw caps
    would rebuild the same hole one file further away, with an operator typing platform.admin
    instead of an attacker."""
    from app.config import Settings

    s = Settings(INTERNAL_SERVICE_TOKEN="x" * 40, REGISTRATION_DEFAULT_ROLES="member, viewer")
    assert s.registration_default_roles == ["member", "viewer"]
    assert not any("." in r for r in s.registration_default_roles)
