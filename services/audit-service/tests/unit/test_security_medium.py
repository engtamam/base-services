"""Security MEDIUM remediation: nested PII redaction (audit-010) and the production boot guards for
PII-off (audit-014) and unprotected retention (audit-005)."""
from __future__ import annotations

import pytest

from app.core.pii_policy import ENFORCE, FOLD, PiiPolicy, PiiPolicyError


# ── audit-010: PII nested in metadata is redacted at every depth ─────────────────
def test_nested_metadata_pii_is_redacted():
    p = PiiPolicy(mode=FOLD, fields=("email",), salt="s" * 16)
    _, _, meta = p.apply(tenant="t", actor="sub_" + "a" * 32, ip="",
                         metadata={"owner": {"email": "a@x.com"}, "list": [{"email": "b@x.com"}]})
    assert meta["owner"]["email"] == "[redacted]"
    assert meta["list"][0]["email"] == "[redacted]"


def test_nested_metadata_pii_is_refused_in_enforce():
    p = PiiPolicy(mode=ENFORCE, fields=("email",), salt="s" * 16)
    with pytest.raises(PiiPolicyError):
        p.apply(tenant="t", actor="sub_" + "a" * 32, ip="", metadata={"deep": {"email": "a@x.com"}})


# ── boot guards ──────────────────────────────────────────────────────────────────
def _settings(**over):
    from app.config import Settings
    base = dict(_env_file=None, PII_SALT="s" * 16)
    base.update(over)
    return Settings(**base)


def test_pii_off_refused_in_production():
    with pytest.raises(ValueError):
        _settings(ENV="production", AUDIT_PII_MODE="off").assert_pii_mode_ok()


def test_pii_off_allowed_with_ack():
    _settings(ENV="production", AUDIT_PII_MODE="off",
              AUDIT_ALLOW_PII_OFF=True).assert_pii_mode_ok()


def test_pii_off_fine_outside_production():
    _settings(ENV="test", AUDIT_PII_MODE="off").assert_pii_mode_ok()


def test_unprotected_retention_refused_in_production():
    with pytest.raises(ValueError):
        _settings(ENV="production", RETENTION_DAYS=30, HOLD_REFS_DRIVER="null") \
            .assert_retention_hold_ok()


def test_unprotected_retention_allowed_with_ack():
    _settings(ENV="production", RETENTION_DAYS=30, HOLD_REFS_DRIVER="null",
              AUDIT_ALLOW_RETENTION_WITHOUT_HOLDS=True).assert_retention_hold_ok()


def test_retention_fine_with_hold_driver():
    _settings(ENV="production", RETENTION_DAYS=30, HOLD_REFS_DRIVER="http",
              COMPLIANCE_SERVICE_URL="http://compliance:8000").assert_retention_hold_ok()
