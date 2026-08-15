"""caps_grant — the one judgement that governs a direct-cap grant.

Direct caps on a membership are unioned into the minted token, so an ungoverned grant is a privilege
escalation. These pin the contract: a caller may only grant what it holds (subset), a roles-only
deployment refuses direct caps outright (off), an allow-list narrows it further (allowlist), and the
internal bootstrap (no granter) is exempt — and the refusal always NAMES the offending cap.
"""
from __future__ import annotations

import pytest

from app.core import caps_grant as cg


def test_subset_allows_a_cap_the_granter_holds():
    cg.check(["reports:read"], granter_caps={"reports:read", "members:write"})   # no raise


def test_subset_refuses_a_cap_the_granter_lacks_by_name():
    with pytest.raises(cg.CapsNotGrantable) as ei:
        cg.check(["platform.operations"], granter_caps={"members:write"})
    assert ei.value.cap == "platform.operations" and ei.value.policy == "subset"


def test_off_refuses_any_direct_cap_even_one_the_granter_holds():
    with pytest.raises(cg.CapsNotGrantable) as ei:
        cg.check(["members:write"], granter_caps={"members:write"}, policy="off")
    assert ei.value.policy == "off"


def test_allowlist_requires_both_granter_and_the_list():
    # in the granter's caps but NOT in the allow-list → refused
    with pytest.raises(cg.CapsNotGrantable):
        cg.check(["reports:read"], granter_caps={"reports:read"}, policy="allowlist",
                 grantable={"members:read"})
    # in both → allowed
    cg.check(["reports:read"], granter_caps={"reports:read"}, policy="allowlist",
             grantable={"reports:read"})


def test_no_caps_is_always_a_noop():
    for policy in ("subset", "off", "allowlist"):
        cg.check([], granter_caps=frozenset(), policy=policy)
        cg.check(None, granter_caps={"x"}, policy=policy)


def test_none_granter_is_the_internal_bootstrap_exemption():
    # seed_owner / internal add-member pass no granter — trusted platform authority, exempt even off.
    cg.check(["members:write", "platform.operations"], granter_caps=None, policy="off")


def test_an_unknown_policy_falls_back_to_subset_never_open():
    with pytest.raises(cg.CapsNotGrantable):
        cg.check(["x"], granter_caps={"y"}, policy="whatever")
