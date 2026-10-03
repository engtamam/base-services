"""Security MEDIUM remediation: destructive /internal boot guard (zatca-018) and admin rate-limit
(zatca-022)."""
from __future__ import annotations

import pytest


def _settings(**over):
    from app.config import Settings
    base = dict(_env_file=None, INTERNAL_SERVICE_TOKEN="z" * 40, PII_SALT="s" * 16)
    base.update(over)
    return Settings(**base)


# ── zatca-018: destructive /internal routes fail-closed at boot in production ──────
def test_boot_guard_refuses_prod_default_open():
    with pytest.raises(ValueError):
        _settings(ENV="production", INTERNAL_ROUTE_POLICY_JSON="").assert_internal_policy_ok()


def test_boot_guard_passes_when_declared():
    pol = ('{"/internal/backup/import": ["backup-service"], "/internal/privacy/erase": ["compliance-service"], '
           '"/internal/zatca/sweep": ["*"], "/internal/zatca/finalize": ["*"]}')
    _settings(ENV="production", INTERNAL_ROUTE_POLICY_JSON=pol).assert_internal_policy_ok()


def test_boot_guard_noop_outside_production():
    _settings(ENV="test", INTERNAL_ROUTE_POLICY_JSON="").assert_internal_policy_ok()


# ── zatca-022: expensive admin routes carry the rate-limit dependency ──────────────
def test_admin_routes_are_rate_limited():
    from app.api.ratelimit import rate_limit
    from app.api.v1.zatca import router
    want = {"/api/v1/zatca/invoices", "/api/v1/zatca/invoices/{invoice_id}/resubmit",
            "/api/v1/zatca/invoices/{invoice_id}/correct-and-resign", "/api/v1/zatca/chain/verify",
            "/api/v1/zatca/sweep"}
    seen = {}
    for r in router.routes:
        p = getattr(r, "path", "")
        if p in want:
            deps = [d.dependency for d in getattr(r, "dependencies", [])]
            seen[p] = rate_limit in deps
    assert want <= set(seen), f"missing: {want - set(seen)}"
    assert all(seen.values()), f"not rate-limited: {[p for p, ok in seen.items() if not ok]}"
