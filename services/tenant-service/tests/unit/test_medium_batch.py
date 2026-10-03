"""Non-security MEDIUM remediation (tenant-service):
  006  TenantErasure no longer advertises an ERASURE_MAX_SCAN/max_scan knob it never read.
  007  the admin catalogue is cursor-paginated (walks every tenant, returns a next cursor).
  008  a malformed route-policy / caller-key JSON is refused at boot, not degraded to default-open.
"""
from __future__ import annotations

import asyncio

import pytest

from app.config import Settings


def run(c):
    return asyncio.run(c)


# ── 006: the dead knob is gone ────────────────────────────────────────────────
def test_tenant_erasure_takes_no_max_scan():
    import inspect
    from app.core.erasure import TenantErasure
    params = inspect.signature(TenantErasure.__init__).parameters
    assert "max_scan" not in params
    assert not hasattr(Settings(_env_file=None, INTERNAL_SERVICE_TOKEN="x" * 40), "ERASURE_MAX_SCAN")


# ── 007: cursor pagination over the catalogue ─────────────────────────────────
def test_list_page_paginates_over_all_tenants():
    from app.adapters.datastore.memory import MemoryDataStore
    from app.core.tenant_repository import TenantRepository, _TENANTS

    st = MemoryDataStore()
    for i in range(5):
        run(st.put(_TENANTS, f"t{i}", {"id": f"t{i}", "slug": f"s{i}", "status": "active",
                                       "created_at": f"2020-01-0{i+1}T00:00:00Z"}))
    repo = TenantRepository(st)
    seen = []
    cursor = None
    for _ in range(10):
        rows, cursor = run(repo.list_page(limit=2, after=cursor))
        seen.extend(r["id"] for r in rows)
        if not cursor:
            break
    assert set(seen) == {"t0", "t1", "t2", "t3", "t4"}   # every tenant, across pages


# ── 008: malformed security maps are refused at boot ──────────────────────────
def _s(**over):
    base = dict(_env_file=None, INTERNAL_SERVICE_TOKEN="x" * 40)
    base.update(over)
    return Settings(**base)


def test_malformed_route_policy_refused():
    with pytest.raises(ValueError, match="INTERNAL_ROUTE_POLICY_JSON"):
        _s(INTERNAL_ROUTE_POLICY_JSON="{not json").assert_internal_caller_config_ok()


def test_malformed_caller_key_map_refused():
    with pytest.raises(ValueError, match="INTERNAL_CALLER_PUBLIC_KEYS_JSON"):
        _s(INTERNAL_CALLER_DRIVER="static",
           INTERNAL_CALLER_PUBLIC_KEYS_JSON="nope").assert_internal_caller_config_ok()


def test_valid_maps_pass():
    _s(INTERNAL_ROUTE_POLICY_JSON='{"/internal/x": ["caller-a"]}').assert_internal_caller_config_ok()
    _s(INTERNAL_CALLER_DRIVER="static",
       INTERNAL_CALLER_PUBLIC_KEYS_JSON='{"identity-service": "PEM"}').assert_internal_caller_config_ok()
