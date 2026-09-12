"""Boot guard for the platform-capability boundary (audit identity-r2-05).

The mint-time strip of '*'/'platform.*' is only well-defined when the service knows which tenant IS
the platform. With multi-tenant membership ON and PLATFORM_TENANT UNSET the boundary is undecidable
and the single-tenant strip fails OPEN, so a stock deployment could mint a customer token carrying
platform authority. The fix refuses to boot in that ambiguous case, with an explicit documented
opt-out (IDENTITY_ALLOW_UNBOUNDED_PLATFORM) for a deliberate single-tenant deployment that intends
'*' to be usable.

A genuinely single-tenant deployment (no membership) is unaffected: nothing here fires.
"""
from __future__ import annotations

import pytest

from app.config import Settings


def _settings(**kw):
    base = dict(ENV="production", PII_SALT="s" * 32)
    base.update(kw)
    return Settings(_env_file=None, **base)


def test_multi_tenant_without_platform_tenant_refuses_to_boot():
    """THE guard: the ambiguous case fails closed."""
    with pytest.raises(ValueError) as e:
        _settings(IDENTITY_MULTI_TENANT=True, PLATFORM_TENANT="").assert_platform_boundary_ok()
    msg = str(e.value)
    assert "PLATFORM_TENANT" in msg and "IDENTITY_MULTI_TENANT" in msg


def test_multi_tenant_with_platform_tenant_boots():
    """A named platform tenant makes the boundary decidable — the strip is well-defined."""
    _settings(IDENTITY_MULTI_TENANT=True, PLATFORM_TENANT="platform").assert_platform_boundary_ok()


def test_explicit_override_allows_unbounded_platform():
    """The deliberate single-tenant-that-runs-membership opt-out: '*' is meant to work here."""
    _settings(IDENTITY_MULTI_TENANT=True, PLATFORM_TENANT="",
              IDENTITY_ALLOW_UNBOUNDED_PLATFORM=True).assert_platform_boundary_ok()


def test_single_tenant_deployment_is_unaffected():
    """No membership means one tenant and no customer to fence off — the guard never fires,
    even with PLATFORM_TENANT empty (the default). Single-tenant behaviour is unchanged."""
    _settings(IDENTITY_MULTI_TENANT=False, PLATFORM_TENANT="").assert_platform_boundary_ok()
