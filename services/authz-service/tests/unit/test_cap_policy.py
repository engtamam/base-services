"""cap_policy.forbidden_caps — the pure rule behind the escalation guard."""
from __future__ import annotations

from app.core.cap_policy import forbidden_caps

PREFIXES = ("platform.",)
DENIED = frozenset({"authz.manage"})


def _f(caps, *, is_operator=False, allowlist=frozenset()):
    return forbidden_caps(caps, is_operator=is_operator, denied_prefixes=PREFIXES,
                          denied_caps=DENIED, allowlist=allowlist)


def test_platform_prefixed_caps_are_forbidden_for_a_tenant_admin():
    assert _f(["platform.authz", "platform.operators"]) == ["platform.authz", "platform.operators"]


def test_reserved_authz_manage_is_forbidden():
    assert _f(["authz.manage"]) == ["authz.manage"]


def test_ordinary_tenant_caps_pass_with_no_allowlist():
    assert _f(["billing.self", "tickets.write"]) == []


def test_an_operator_is_never_restricted():
    assert _f(["platform.authz", "authz.manage"], is_operator=True) == []


def test_allowlist_rejects_anything_outside_it():
    assert _f(["billing.self", "reports.export"], allowlist=frozenset({"billing.self"})) == \
        ["reports.export"]


def test_denylist_beats_allowlist():
    # even if someone lists a platform cap in the allowlist, the prefix deny still refuses it
    assert _f(["platform.authz"], allowlist=frozenset({"platform.authz"})) == ["platform.authz"]


def test_result_is_deduplicated_and_order_preserving():
    assert _f(["platform.a", "platform.a", "platform.b"]) == ["platform.a", "platform.b"]


def test_blank_caps_are_ignored():
    assert _f(["", "  "]) == []
