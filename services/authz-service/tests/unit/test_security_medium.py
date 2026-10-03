"""Security MEDIUM remediation: wildcard-aware operator detection (authz-006) and fail-fast config
JSON parsing (authz-010)."""
from __future__ import annotations

import pytest

from app.core.cap_policy import holds_cap


# ── authz-006: a wildcard holder is an operator, like the PDP sees them ───────────
def test_holds_cap_exact():
    assert holds_cap("platform.authz", ["platform.authz"]) is True


def test_holds_cap_god_wildcard():
    assert holds_cap("platform.authz", ["*"]) is True


def test_holds_cap_prefix_wildcard():
    assert holds_cap("platform.authz", ["platform.*"]) is True


def test_holds_cap_unrelated():
    assert holds_cap("platform.authz", ["billing.read", "files.*"]) is False


def test_is_operator_uses_wildcard():
    from app.api.auth import Principal
    from app.api.v1.authz import _is_operator
    assert _is_operator(Principal(subject="u", caps=frozenset({"*"}), tenant="t")) is True
    assert _is_operator(Principal(subject="u", caps=frozenset({"platform.*"}), tenant="t")) is True
    assert _is_operator(Principal(subject="u", caps=frozenset({"authz.manage"}), tenant="t")) is False


# ── authz-010: malformed security config fails closed, not silently empty ─────────
def test_json_map_raises_on_malformed():
    from app.config import _json_any_map
    with pytest.raises(ValueError):
        _json_any_map('{"a":')


def test_json_map_empty_is_ok():
    from app.config import _json_any_map
    assert _json_any_map("") == {}
    assert _json_any_map("  ") == {}
