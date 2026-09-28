from __future__ import annotations

import asyncio

import pytest

from app.core.tenant_status import enforce_tenant_active
from app.ports.tenant_gate import (
    ACTIVE,
    DELETED,
    SUSPENDED,
    UNKNOWN,
    TenantGateUnavailable,
    TenantInactive,
)


def run(c):
    return asyncio.run(c)


class _Gate:
    driver = "fake"

    def __init__(self, status=ACTIVE, *, raises=False):
        self._status = status
        self._raises = raises
        self.calls = 0

    async def status(self, tenant):
        self.calls += 1
        if self._raises:
            raise TenantGateUnavailable("boom")
        return self._status


def _enforce(gate, tenant="acme", *, enforcement="block", platform_tenant="platform",
             fail_mode="open", block_unknown=True):
    return run(enforce_tenant_active(gate, tenant, enforcement=enforcement,
                                     platform_tenant=platform_tenant, fail_mode=fail_mode,
                                     block_unknown=block_unknown))


# ── policy ────────────────────────────────────────────────────────────────────
def test_enforcement_off_never_calls_the_gate():
    g = _Gate(SUSPENDED)
    _enforce(g, enforcement="off")
    assert g.calls == 0                                  # off is inert, no lookup at all


def test_active_tenant_is_allowed():
    _enforce(_Gate(ACTIVE))                              # no raise


def test_suspended_tenant_is_blocked():
    with pytest.raises(TenantInactive) as ei:
        _enforce(_Gate(SUSPENDED))
    assert ei.value.tenant_status == SUSPENDED


def test_deleted_tenant_is_blocked():
    with pytest.raises(TenantInactive):
        _enforce(_Gate(DELETED))


def test_platform_tenant_is_never_gated():
    g = _Gate(SUSPENDED)
    _enforce(g, tenant="platform")                      # operator plane, even if it looked suspended
    assert g.calls == 0


def test_unknown_is_blocked_when_configured():
    with pytest.raises(TenantInactive):
        _enforce(_Gate(UNKNOWN), block_unknown=True)


def test_unknown_is_allowed_when_not_blocking_unknown():
    _enforce(_Gate(UNKNOWN), block_unknown=False)       # no raise


def test_unavailable_fails_open_by_default():
    _enforce(_Gate(raises=True), fail_mode="open")      # availability: a blip must not 401 everyone


def test_unavailable_fails_closed_when_configured():
    with pytest.raises(TenantGateUnavailable):
        _enforce(_Gate(raises=True), fail_mode="closed")


def test_empty_tenant_is_not_gated():
    g = _Gate(SUSPENDED)
    _enforce(g, tenant="")                               # a token with no tenant claim is not a tenant
    assert g.calls == 0


# ── HttpTenantGate (transport parsing + caching) ────────────────────────────────
class _Resp:
    def __init__(self, code, body=None):
        self.status_code = code
        self._body = body or {}

    def json(self):
        return self._body


class _Client:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        return self._resp


class _Httpx:
    def __init__(self, resp):
        self._resp = resp
        self.gets = 0

    def AsyncClient(self, timeout=None):
        self.gets += 1
        return _Client(self._resp)


class _Cache:
    driver = "memory"

    def __init__(self):
        self.d = {}

    async def get(self, k):
        return self.d.get(k)

    async def set(self, k, v, ttl=None):
        self.d[k] = v


def _gate(resp):
    from app.adapters.tenant_gate.tenant import HttpTenantGate
    from app.config import get_settings
    g = HttpTenantGate("http://tenant:8000", "x" * 32, cache=_Cache(), settings=get_settings(),
                       timeout_ms=800, ttl_seconds=30)
    g._httpx = _Httpx(resp)
    return g


def test_http_gate_200_returns_and_caches_status():
    g = _gate(_Resp(200, {"status": "suspended"}))
    assert run(g.status("acme")) == SUSPENDED
    assert run(g.status("acme")) == SUSPENDED            # second call served from cache
    assert g._httpx.gets == 1                            # only one HTTP round-trip


def test_http_gate_404_is_unknown():
    g = _gate(_Resp(404))
    assert run(g.status("ghost")) == UNKNOWN


def test_http_gate_500_is_unavailable():
    g = _gate(_Resp(500))
    with pytest.raises(TenantGateUnavailable):
        run(g.status("acme"))


def test_http_gate_malformed_status_is_unavailable():
    g = _gate(_Resp(200, {"status": "weird"}))
    with pytest.raises(TenantGateUnavailable):
        run(g.status("acme"))


# ── boot guard ──────────────────────────────────────────────────────────────────
def _settings(**over):
    from app.config import Settings
    base = dict(INTERNAL_SERVICE_TOKEN="z" * 40, PII_SALT="s" * 16)
    base.update(over)
    return Settings(**base)


def test_boot_guard_block_without_driver_refuses():
    s = _settings(TENANT_STATUS_ENFORCEMENT="block", TENANT_GATE_DRIVER="off")
    with pytest.raises(ValueError):
        s.assert_tenant_status_config_ok()


def test_boot_guard_block_without_url_refuses():
    s = _settings(TENANT_STATUS_ENFORCEMENT="block", TENANT_GATE_DRIVER="tenant",
                  TENANT_SERVICE_URL="")
    with pytest.raises(ValueError):
        s.assert_tenant_status_config_ok()


def test_boot_guard_block_fully_wired_passes():
    s = _settings(TENANT_STATUS_ENFORCEMENT="block", TENANT_GATE_DRIVER="tenant",
                  TENANT_SERVICE_URL="http://tenant-service:8000")
    s.assert_tenant_status_config_ok()                  # no raise


def test_boot_guard_off_is_always_fine():
    _settings().assert_tenant_status_config_ok()        # default off, nothing required


# ── require_user wiring: a suspended tenant's valid token is refused at the gateway ──
class _Verifier:
    def verify(self, token):
        return {"sub": "u1", "tenant": "acme", "caps": ["x"]}


class _State:
    pass


class _App:
    def __init__(self, gate):
        self.state = _State()
        self.state.container = _State()
        self.state.container.token_verifier = _Verifier()
        self.state.container.tenant_gate = gate


class _Req:
    def __init__(self, app):
        self.app = app
        self.headers = {"authorization": "Bearer good"}
        self.state = _State()


class _EnfSettings:
    TENANT_STATUS_ENFORCEMENT = "block"
    PLATFORM_TENANT = "platform"
    TENANT_STATUS_FAIL_MODE = "open"
    TENANT_STATUS_BLOCK_UNKNOWN = True


def test_require_user_403s_a_suspended_tenant(monkeypatch):
    import app.api.auth as auth
    from fastapi import HTTPException
    monkeypatch.setattr(auth, "get_settings", lambda: _EnfSettings())
    req = _Req(_App(_Gate(SUSPENDED)))
    with pytest.raises(HTTPException) as ei:
        run(auth.require_user(req))
    assert ei.value.status_code == 403 and ei.value.detail == "TENANT_INACTIVE"


def test_require_user_allows_an_active_tenant(monkeypatch):
    import app.api.auth as auth
    monkeypatch.setattr(auth, "get_settings", lambda: _EnfSettings())
    req = _Req(_App(_Gate(ACTIVE)))
    principal = run(auth.require_user(req))
    assert principal.tenant == "acme" and principal.subject == "u1"
