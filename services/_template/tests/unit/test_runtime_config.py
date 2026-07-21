"""Runtime policy overrides.

The important tests are the refusals: what an operator must NOT be able to change from a panel, and
what must be rejected there for the same reason it is rejected at boot.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.config import Settings
from app.core.runtime_config import RuntimeConfig, RuntimeConfigError, settable


def run(c):
    return asyncio.run(c)


def _rc(validate=None):
    return RuntimeConfig(MemoryDataStore(), Settings, now=lambda: "2026-01-01T00:00:00Z",
                         validate=validate)


def _settings():
    return Settings(INTERNAL_SERVICE_TOKEN="x" * 40)


# ── what may never be set from a panel ────────────────────────────────────────
@pytest.mark.parametrize("key", [
    "DATASTORE_DRIVER",          # wiring: an adapter changing under a live request
    "DATASTORE_DSN",
    "CACHE_URL",
    "INTERNAL_SERVICE_TOKEN",    # a secret
    "PII_SALT",                  # frozen identifier
    "SERVICE_NAME",
    "ENV",
])
def test_wiring_and_secrets_are_refused(key):
    assert not settable(key, Settings)
    with pytest.raises(RuntimeConfigError, match="deploy-time"):
        run(_rc().set(tenant="acme", key=key, value="anything"))


def test_an_unknown_knob_is_refused():
    # A typo must not be silently stored as an override nothing reads.
    with pytest.raises(RuntimeConfigError):
        run(_rc().set(tenant="acme", key="NOT_A_REAL_SETTING", value="1"))


def test_the_refusal_does_not_reveal_which_internal_settings_exist():
    """A real knob that is not settable and a knob that does not exist must give the SAME reason.

    Distinguishing them turns the panel into an oracle for the internal settings list. Echoing
    back the key the caller themselves sent is fine — they already know it — so the assertion is
    on the reason, with each caller's own key removed.
    """
    rc = _rc()
    with pytest.raises(RuntimeConfigError) as a:
        run(rc.set(tenant="t", key="DATASTORE_DSN", value="x"))
    with pytest.raises(RuntimeConfigError) as b:
        run(rc.set(tenant="t", key="TOTALLY_INVENTED", value="x"))
    reason_a = str(a.value).replace("DATASTORE_DSN", "<key>")
    reason_b = str(b.value).replace("TOTALLY_INVENTED", "<key>")
    assert reason_a == reason_b, "the message says whether the setting exists"


# ── what may be set ───────────────────────────────────────────────────────────
def test_an_ordinary_policy_value_is_settable():
    rc = _rc()
    o = run(rc.set(tenant="acme", key="RATELIMIT_MAX", value=250))
    assert o.value == 250
    assert run(rc.get(tenant="acme", key="RATELIMIT_MAX", default=100)) == 250


def test_config_is_the_default_when_nothing_is_overridden():
    assert run(_rc().get(tenant="acme", key="RATELIMIT_MAX", default=100)) == 100


def test_a_tenant_override_beats_the_platform_one():
    rc = _rc()
    run(rc.set(tenant="", key="RATELIMIT_MAX", value=200))
    run(rc.set(tenant="acme", key="RATELIMIT_MAX", value=50))
    assert run(rc.get(tenant="acme", key="RATELIMIT_MAX", default=100)) == 50
    assert run(rc.get(tenant="globex", key="RATELIMIT_MAX", default=100)) == 200


def test_clearing_restores_the_configured_default():
    rc = _rc()
    run(rc.set(tenant="acme", key="RATELIMIT_MAX", value=250))
    assert run(rc.clear(tenant="acme", key="RATELIMIT_MAX")) is True
    assert run(rc.get(tenant="acme", key="RATELIMIT_MAX", default=100)) == 100


# ── typing ────────────────────────────────────────────────────────────────────
def test_a_value_is_coerced_to_the_declared_type():
    # "250" stored as a string passes validation and fails later, inside whatever compares it.
    rc = _rc()
    assert run(rc.set(tenant="acme", key="RATELIMIT_MAX", value="250")).value == 250


def test_a_value_of_the_wrong_type_is_refused_now_rather_than_later():
    with pytest.raises(RuntimeConfigError, match="not a valid"):
        run(_rc().set(tenant="acme", key="RATELIMIT_MAX", value="ninety"))


@pytest.mark.parametrize("raw,expect", [("true", True), ("false", False), (True, True)])
def test_booleans_accept_what_an_operator_would_type(raw, expect):
    assert run(_rc().set(tenant="acme", key="AUDIT_FAIL_CLOSED", value=raw)).value is expect


def test_a_nonsense_boolean_is_refused():
    with pytest.raises(RuntimeConfigError):
        run(_rc().set(tenant="acme", key="AUDIT_FAIL_CLOSED", value="maybe"))


# ── the boot guard applies here too ───────────────────────────────────────────
def test_the_boot_guards_rule_is_enforced_at_runtime():
    """Validating only at startup means the rule lasts until somebody finds the settings page, and
    the resulting deployment is one no boot guard would have allowed."""
    def guard(key, value):
        if key == "RATELIMIT_MAX" and value < 1:
            raise RuntimeConfigError("RATELIMIT_MAX must be at least 1")

    rc = _rc(validate=guard)
    with pytest.raises(RuntimeConfigError, match="at least 1"):
        run(rc.set(tenant="acme", key="RATELIMIT_MAX", value=0))


def test_a_value_the_guard_accepts_is_stored():
    def guard(key, value):
        if key == "RATELIMIT_MAX" and value < 1:
            raise RuntimeConfigError("too low")

    rc = _rc(validate=guard)
    assert run(rc.set(tenant="acme", key="RATELIMIT_MAX", value=5)).value == 5


# ── what the panel sees ───────────────────────────────────────────────────────
def test_the_effective_view_never_contains_a_secret_or_a_dsn():
    rc = _rc()
    view = run(rc.effective(tenant="acme", settings=_settings()))
    for banned in ("INTERNAL_SERVICE_TOKEN", "PII_SALT", "DATASTORE_DSN", "CACHE_URL"):
        assert banned not in view


def test_the_effective_view_shows_overrides_applied():
    rc = _rc()
    run(rc.set(tenant="acme", key="RATELIMIT_MAX", value=77))
    assert run(rc.effective(tenant="acme", settings=_settings()))["RATELIMIT_MAX"] == 77


def test_a_corrupt_override_falls_back_instead_of_taking_the_service_down():
    # A broken row is an operator problem; it must not become an outage.
    rc = _rc()
    run(rc.set(tenant="acme", key="RATELIMIT_MAX", value=7))
    row = run(rc._s.get("runtime_config", "acme|RATELIMIT_MAX"))
    row["value"] = "{not json"
    run(rc._s.put("runtime_config", "acme|RATELIMIT_MAX", row))
    assert run(rc.get(tenant="acme", key="RATELIMIT_MAX", default=100)) == 100


def test_overrides_are_listed_with_who_set_them():
    rc = _rc()
    run(rc.set(tenant="acme", key="RATELIMIT_MAX", value=42, actor_ref="sub_abc"))
    got = run(rc.overrides(tenant="acme"))
    assert got[0].key == "RATELIMIT_MAX" and got[0].set_by == "sub_abc"
