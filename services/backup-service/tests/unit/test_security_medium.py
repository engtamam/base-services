"""Security MEDIUM remediation: SSRF resolution guard (backup-007) and own-secrets backup (backup-012)."""
from __future__ import annotations

import pytest

from app.core.internal_http import TargetUnreachable, _check_host


# ── backup-007: an allow-listed name that resolves to metadata/loopback is refused ──
def test_allowlisted_name_resolving_to_loopback_is_blocked():
    # localhost resolves to 127.0.0.1 (loopback) — even allow-listed, a token-bearing call must not go
    with pytest.raises(TargetUnreachable):
        _check_host("http://localhost:8000/x", frozenset({"localhost"}))


def test_metadata_literal_is_blocked():
    with pytest.raises(TargetUnreachable):
        _check_host("http://169.254.169.254/x", frozenset({"169.254.169.254"}))


def test_unlisted_host_still_refused():
    with pytest.raises(TargetUnreachable):
        _check_host("https://evil.example/x", frozenset({"tenant-service"}))


# ── backup-012: backup-service backs up its own operator credentials ──────────────
def test_operator_secrets_are_in_the_backup_manifest():
    from app.backup_manifest import BACKUP
    names = {c["name"] for c in BACKUP["collections"]}
    assert "operator_secrets" in names
