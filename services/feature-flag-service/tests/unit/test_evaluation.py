"""Unit: rollout math, rule matchers, precedence chain, fail-safe."""
from __future__ import annotations

from app.core.evaluate import evaluate
from app.core.rollout import BUCKETS, bucket, in_rollout
from app.core.rules import first_match, match_condition, match_rule

SALT = "s"


def _ctx(tenant="", user="", attrs=None):
    return {"tenant": tenant, "user": user, "attrs": attrs or {}}


# ── rollout ───────────────────────────────────────────────────────────────────
def test_bucket_is_deterministic_and_bounded():
    assert bucket(SALT, "f", "u1") == bucket(SALT, "f", "u1")      # sticky
    assert 0 <= bucket(SALT, "f", "u1") < BUCKETS
    assert bucket(SALT, "f", "u1") != bucket(SALT, "f", "u2") or True   # different subjects may differ


def test_rollout_distribution_is_roughly_uniform():
    n = 5000
    on = sum(in_rollout(SALT, "f", f"user{i}", 50) for i in range(n))
    assert 0.45 < on / n < 0.55                                     # ~50%


def test_flags_do_not_correlate():
    n = 2000
    a = [in_rollout(SALT, "flagA", f"u{i}", 50) for i in range(n)]
    b = [in_rollout(SALT, "flagB", f"u{i}", 50) for i in range(n)]
    agree = sum(x == y for x, y in zip(a, b)) / n
    assert 0.40 < agree < 0.60          # ~50% — the flag key salts the hash


def test_rollout_edges():
    assert in_rollout(SALT, "f", "u", 100) is True
    assert in_rollout(SALT, "f", "u", 0) is False
    assert in_rollout(SALT, "f", "", 50) is False                    # no subject → not in rollout


def test_salt_change_rebuckets():
    assert bucket("saltA", "f", "u1") != bucket("saltB", "f", "u1")


# ── rules ─────────────────────────────────────────────────────────────────────
def test_matchers():
    a = {"country": "SA", "plan": "pro", "seats": 12, "email": "a@acme.com"}
    assert match_condition({"attribute": "country", "op": "equals", "value": "SA"}, a)
    assert match_condition({"attribute": "country", "op": "in", "value": ["SA", "AE"]}, a)
    assert match_condition({"attribute": "country", "op": "not_in", "value": ["EG"]}, a)
    assert match_condition({"attribute": "email", "op": "suffix", "value": "@acme.com"}, a)
    assert match_condition({"attribute": "email", "op": "contains", "value": "acme"}, a)
    assert match_condition({"attribute": "seats", "op": "gte", "value": 10}, a)
    assert not match_condition({"attribute": "seats", "op": "lt", "value": 10}, a)


def test_matchers_fail_closed():
    a = {"x": 1}
    assert not match_condition({"attribute": "missing", "op": "equals", "value": 1}, a)
    assert not match_condition({"attribute": "x", "op": "exec", "value": 1}, a)      # unknown op
    assert not match_condition({"attribute": "x", "op": "gt", "value": "abc"}, a)    # non-numeric
    assert not match_rule({"conditions": []}, a)                                     # empty rule


def test_rule_conditions_are_anded_and_first_match_wins():
    attrs = {"country": "SA", "plan": "pro"}
    r1 = {"conditions": [{"attribute": "country", "op": "equals", "value": "EG"}], "enabled": True}
    r2 = {"conditions": [{"attribute": "country", "op": "equals", "value": "SA"},
                         {"attribute": "plan", "op": "equals", "value": "pro"}], "enabled": True, "variant": "v2"}
    assert first_match([r1, r2], attrs) is r2
    r2["conditions"][1]["value"] = "free"           # now the AND fails
    assert first_match([r1, r2], attrs) is None


# ── precedence ────────────────────────────────────────────────────────────────
FLAG = {"key": "f", "enabled": True, "default": False, "rollout_pct": 0,
        "user_overrides": {"vip": True}, "tenant_overrides": {"t2": True},
        "rules": [{"conditions": [{"attribute": "country", "op": "in", "value": ["SA"]}],
                   "enabled": True, "variant": "ksa"}],
        "requires_entitlement": "export"}


def test_unknown_flag_is_fail_safe():
    assert evaluate(None, _ctx(user="u"), salt=SALT) == {"enabled": False, "reason": "unknown_flag", "variant": ""}


def test_kill_switch_beats_everything():
    assert evaluate({**FLAG, "enabled": False}, _ctx(user="vip"), salt=SALT)["reason"] == "kill_switch"
    assert evaluate({**FLAG, "archived": True}, _ctx(user="vip"), salt=SALT)["reason"] == "kill_switch"


def test_entitlement_denied_beats_overrides():
    r = evaluate(FLAG, _ctx(user="vip"), salt=SALT, entitled=False)
    assert r["enabled"] is False and r["reason"] == "entitlement_denied"


def test_user_override_beats_tenant():
    f = {**FLAG, "requires_entitlement": "", "user_overrides": {"bob": False}, "tenant_overrides": {"t1": True}}
    r = evaluate(f, _ctx(tenant="t1", user="bob"), salt=SALT)
    assert r["enabled"] is False and r["reason"] == "user_override"


def test_tenant_override_then_rule_then_default():
    f = {**FLAG, "requires_entitlement": ""}
    assert evaluate(f, _ctx(tenant="t2", user="bob"), salt=SALT)["reason"] == "tenant_override"
    r = evaluate(f, _ctx(user="bob", attrs={"country": "SA"}), salt=SALT)
    assert r["reason"] == "rule_match" and r["variant"] == "ksa"
    assert evaluate(f, _ctx(user="bob", attrs={"country": "EG"}), salt=SALT)["reason"] == "default"


def test_rollout_reason():
    f = {**FLAG, "requires_entitlement": "", "rules": [], "user_overrides": {}, "tenant_overrides": {},
         "rollout_pct": 100}
    assert evaluate(f, _ctx(user="bob"), salt=SALT)["reason"] == "rollout"


def test_entitlement_only_checked_when_required():
    f = {**FLAG, "requires_entitlement": "", "rollout_pct": 100, "rules": [], "user_overrides": {}}
    assert evaluate(f, _ctx(user="bob"), salt=SALT, entitled=False)["enabled"] is True
