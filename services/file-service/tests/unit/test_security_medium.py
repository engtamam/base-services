"""Security MEDIUM remediation: untenanted-file cross-tenant audit (file-025)."""
from __future__ import annotations

from app.api.auth import Principal
from app.api.v1.files import _crosses_tenant
from app.core.models import FileObject


def _f(tenant):
    return FileObject(id="f1", tenant=tenant, owner="o", filename="x", content_type="text/plain",
                      size=1, sha256="h")


def _op(tenant):
    return Principal(subject="op", caps=frozenset({"platform.files"}), tenant=tenant)


# ── file-025: an untenanted file read by an operator is always auditable ──────────
def test_untenanted_file_crosses_tenant_even_when_operator_tenant_also_empty():
    # operator with no tenant reading a tenant='' file: equality check misses it, so flag on ''
    assert _crosses_tenant(_f(""), _op(None)) is True
    assert _crosses_tenant(_f(""), _op("")) is True


def test_other_tenant_file_still_crosses():
    assert _crosses_tenant(_f("acme"), _op("other")) is True


def test_own_tenant_file_is_not_cross_tenant():
    assert _crosses_tenant(_f("acme"), _op("acme")) is False


def test_non_operator_never_flagged():
    assert _crosses_tenant(_f(""), Principal(subject="u", caps=frozenset(), tenant="acme")) is False
