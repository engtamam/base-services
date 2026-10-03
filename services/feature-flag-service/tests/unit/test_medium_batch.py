"""Non-security MEDIUM remediation: defensive start/end coercion (feature-flag-013), now default
(feature-flag-014), rollout-salt boot guard (feature-flag-011)."""
from __future__ import annotations

import pytest

from app.core.evaluate import evaluate, in_schedule
from app.core.models import now_ts


# ── feature-flag-013: a non-numeric start/end must not throw on the hot path ──────
def test_non_numeric_schedule_does_not_throw():
    assert in_schedule({"start": "oops", "end": None}, now_ts()) is True
    r = evaluate({"key": "k", "enabled": True, "default": True, "start": "bad"},
                 {"user": "u"}, salt="s")
    assert "reason" in r                                   # degraded, did not raise


# ── feature-flag-014: a scheduled flag evaluates against the real clock by default ──
def test_now_defaults_to_real_clock_for_scheduled_flag():
    # start is in the past; with the old now=0.0 default this read as before-start (scheduled/default).
    past = now_ts() - 3600
    r = evaluate({"key": "k", "enabled": True, "default": False, "start": past, "rollout_pct": 100},
                 {"user": "u"}, salt="s")
    assert r["reason"] != "scheduled"                     # the window is open, not treated as before-start


# ── feature-flag-011: rollout-salt boot guard ─────────────────────────────────────
def _settings(**over):
    from app.config import Settings
    base = dict(_env_file=None, INTERNAL_SERVICE_TOKEN="z" * 40, PII_SALT="s" * 16, CACHE_DRIVER="redis",
                CACHE_URL="redis://x")
    base.update(over)
    return Settings(**base)


def test_default_rollout_salt_refused_in_production():
    with pytest.raises(ValueError):
        _settings(ENV="production", ROLLOUT_SALT="base-services").assert_rollout_salt_ok()


def test_custom_rollout_salt_is_fine():
    _settings(ENV="production", ROLLOUT_SALT="deployment-specific-xyz").assert_rollout_salt_ok()


def test_default_salt_fine_in_dev():
    _settings(ENV="test").assert_rollout_salt_ok()
