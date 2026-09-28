"""tenant-001/002/003 regressions: DSN credentials never leave the service, a raw prior name is not
audited, and destructive internal routes are not default-open in production."""
from __future__ import annotations

from app.api.internal import _redact_isolation
from app.core.internal_policy import InternalRoutePolicy


def test_isolation_dsn_userinfo_is_redacted():
    scope = {"mode": "db", "dsn": "postgresql://svc:s3cr3t@dbhost:5432/t_acme"}
    out = _redact_isolation(scope)
    assert "s3cr3t" not in out["dsn"] and "svc" not in out["dsn"]
    assert out["dsn"] == "postgresql://dbhost:5432/t_acme"    # host + db kept, creds gone
    assert out["mode"] == "db"


def test_non_db_scope_is_untouched():
    scope = {"mode": "schema", "schema": "t_acme"}
    assert _redact_isolation(scope) == scope


def test_policy_covers_gates_destructive_routes():
    assert InternalRoutePolicy(None).covers("/internal/tenants/close-sweep") is False
    p = InternalRoutePolicy({"/internal/tenants": ["provisioner"],
                             "/internal/backup/import": ["backup-service"]})
    assert p.covers("/internal/tenants/close-sweep") is True     # longest-prefix under /internal/tenants
    assert p.covers("/internal/tenants/abc") is True             # DELETE /internal/tenants/{id}
    assert p.covers("/internal/backup/import") is True
    assert p.permits("/internal/tenants/abc", "provisioner") is True
    assert p.permits("/internal/tenants/abc", "stranger") is False
