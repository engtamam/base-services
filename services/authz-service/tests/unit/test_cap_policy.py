"""cap_policy.forbidden_caps — the pure rule behind the escalation guard."""
from __future__ import annotations

from app.core.cap_policy import caps_exceeding_granter, forbidden_caps

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


# ── caps_exceeding_granter — the caps<=granter subset rule ─────────────────────
def test_subset_refuses_a_cap_the_granter_does_not_hold():
    # caller holds {a,b}, grants {a,c} -> only c is refused
    assert caps_exceeding_granter(["a", "c"], frozenset({"a", "b"})) == ["c"]


def test_subset_allows_exactly_the_held_caps():
    assert caps_exceeding_granter(["a", "b"], frozenset({"a", "b"})) == []


def test_subset_star_grants_anything():
    assert caps_exceeding_granter(["wallet.payout", "x"], frozenset({"*"})) == []


def test_subset_wildcard_prefix_grants_within_namespace():
    assert caps_exceeding_granter(["files.read", "files.write"], frozenset({"files.*"})) == []
    assert caps_exceeding_granter(["wallet.payout"], frozenset({"files.*"})) == ["wallet.payout"]


def test_subset_empty_granter_refuses_everything():
    assert caps_exceeding_granter(["a", "b"], frozenset()) == ["a", "b"]


def test_subset_is_deduplicated_and_order_preserving():
    assert caps_exceeding_granter(["c", "c", "d"], frozenset({"a"})) == ["c", "d"]
