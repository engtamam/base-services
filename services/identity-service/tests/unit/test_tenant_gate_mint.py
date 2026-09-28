from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.login_identifier import UsernameIdentifier
from app.adapters.mfa.null import NullMfa
from app.adapters.password_hasher.argon2 import Argon2Hasher
from app.adapters.token_issuer.hs256 import HS256Issuer
from app.adapters.token_verifier.hs256 import HS256Verifier
from app.core.auth_service import AuthService
from app.core.refresh_store import RefreshStore
from app.core.user_repository import UserRepository
from app.ports.tenant_gate import ACTIVE, DELETED, SUSPENDED, UNKNOWN, TenantGateUnavailable

SEC = "k" * 40


def run(c):
    return asyncio.run(c)


class _Gate:
    driver = "fake"

    def __init__(self, status=ACTIVE, *, raises=False):
        self._status = status
        self._raises = raises

    async def status(self, tenant):
        if self._raises:
            raise TenantGateUnavailable("boom")
        return self._status


def _svc(gate=None, *, enforcement="off", fail_mode="open", block_unknown=True, platform_tenant=""):
    store = MemoryDataStore()
    return AuthService(
        users=UserRepository(store, UsernameIdentifier()), refresh=RefreshStore(store),
        hasher=Argon2Hasher(), issuer=HS256Issuer(SEC), verifier=HS256Verifier(SEC),
        mfa=NullMfa(), identifier=UsernameIdentifier(), access_ttl=900, refresh_ttl=3600,
        platform_tenant=platform_tenant,
        tenant_gate=gate, tenant_enforcement=enforcement, tenant_fail_mode=fail_mode,
        tenant_block_unknown=block_unknown)


def _user(svc, tenant="acme"):
    return run(svc.register(tenant, "alice", "password1", ["read"]))


def test_off_mints_even_for_a_suspended_tenant():
    svc = _svc(_Gate(SUSPENDED), enforcement="off")   # inert: the gap is only closed when enabled
    toks = run(svc.issue_tokens(_user(svc)))
    assert toks["access_token"]


def test_active_tenant_mints():
    svc = _svc(_Gate(ACTIVE), enforcement="block")
    assert run(svc.issue_tokens(_user(svc)))["access_token"]


def test_suspended_tenant_mints_nothing():
    svc = _svc(_Gate(SUSPENDED), enforcement="block")
    with pytest.raises(AuthService.TenantInactive) as ei:
        run(svc.issue_tokens(_user(svc)))
    assert ei.value.tenant_status == SUSPENDED


def test_deleted_tenant_mints_nothing():
    svc = _svc(_Gate(DELETED), enforcement="block")
    with pytest.raises(AuthService.TenantInactive):
        run(svc.issue_tokens(_user(svc)))


def test_refresh_of_a_suspended_tenant_is_refused():
    # A session must not outlive its tenant's suspension on renewals — refresh mints, so it gates too.
    svc = _svc(_Gate(ACTIVE), enforcement="block")
    toks = run(svc.issue_tokens(_user(svc)))
    svc._tenant_gate = _Gate(SUSPENDED)                # tenant suspended after login
    with pytest.raises(AuthService.TenantInactive):
        run(svc.refresh(toks["refresh_token"]))


def test_platform_tenant_is_never_gated():
    svc = _svc(_Gate(SUSPENDED), enforcement="block", platform_tenant="platform")
    assert run(svc.issue_tokens(_user(svc, tenant="platform")))["access_token"]


def test_unknown_blocks_by_default():
    svc = _svc(_Gate(UNKNOWN), enforcement="block")
    with pytest.raises(AuthService.TenantInactive):
        run(svc.issue_tokens(_user(svc)))


def test_unknown_allowed_when_not_blocking_unknown():
    svc = _svc(_Gate(UNKNOWN), enforcement="block", block_unknown=False)
    assert run(svc.issue_tokens(_user(svc)))["access_token"]


def test_unavailable_fails_open_by_default():
    svc = _svc(_Gate(raises=True), enforcement="block", fail_mode="open")
    assert run(svc.issue_tokens(_user(svc)))["access_token"]


def test_unavailable_fails_closed_when_configured():
    svc = _svc(_Gate(raises=True), enforcement="block", fail_mode="closed")
    with pytest.raises(TenantGateUnavailable):
        run(svc.issue_tokens(_user(svc)))


# ── boot guard ──────────────────────────────────────────────────────────────────
def _settings(**over):
    from app.config import Settings
    base = dict(INTERNAL_SERVICE_TOKEN="z" * 40, PII_SALT="s" * 16)
    base.update(over)
    return Settings(**base)


def test_boot_guard_block_without_url_refuses():
    with pytest.raises(ValueError):
        _settings(IDENTITY_TENANT_STATUS_ENFORCEMENT="block", TENANT_SERVICE_URL="") \
            .assert_tenant_status_config_ok()


def test_boot_guard_block_fully_wired_passes():
    _settings(IDENTITY_TENANT_STATUS_ENFORCEMENT="block",
              TENANT_SERVICE_URL="http://tenant-service:8000").assert_tenant_status_config_ok()


def test_boot_guard_off_is_fine():
    _settings().assert_tenant_status_config_ok()
