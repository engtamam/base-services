"""Operator-managed credentials.

The tests that matter are the ones about what CANNOT happen: no plaintext in the store, no value in
anything the panel can see, no silent acceptance of a wrong key, and no rotation that strands a
request in flight.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.secret_store import SecretError, SecretStore
from app.ports.errors import ConfigError

KEK = "k" * 40
SECRET = "sk-live-abcdefghijklmnop3f2a"


def run(c):
    return asyncio.run(c)


def _store(kek=KEK, **kw):
    return SecretStore(MemoryDataStore(), kek, **kw)


def _raw(st):
    """Everything actually written, as text — for asserting what is NOT in there."""
    return repr(getattr(st._s, "_data", st._s.__dict__))


# ── the KEK ───────────────────────────────────────────────────────────────────
def test_an_empty_kek_is_refused():
    """A store that accepted an empty key would write plaintext-equivalent rows while every check
    reported 'encrypted'."""
    with pytest.raises(ConfigError, match="SECRETS_KEK"):
        SecretStore(MemoryDataStore(), "")


def test_the_wrong_kek_raises_rather_than_returning_nothing():
    # Returning "" would fail every provider call with "unauthorised" and send someone to the
    # provider's dashboard instead of to the KEK they just changed.
    st = _store()
    run(st.set(tenant="acme", name="sms_key", value=SECRET))
    other = SecretStore(st._s, "different-kek-entirely")
    with pytest.raises(SecretError, match="KEK"):
        run(other.get(tenant="acme", name="sms_key"))


def test_a_sealed_credential_cannot_be_relocated_to_another_tenant_or_name():
    """AAD binding (safety:aesgcm-aad): the KEK is one per deployment, so a DB tamperer with no KEK
    could otherwise copy tenant A's sealed blob into tenant B's row (or under another name) and it
    would still decrypt — B authenticating as A. AAD = collection|tenant|name, so a relocated or
    renamed blob fails the GCM tag."""
    st = _store()
    run(st.set(tenant="acme", name="sms_key", value=SECRET))
    row = run(st._s.get("operator_secrets", "acme|sms_key"))

    # Copy the sealed blob into another TENANT's row → open must fail under that tenant's AAD.
    run(st._s.put("operator_secrets", "beta|sms_key",
                  {**row, "id": "beta|sms_key", "tenant": "beta"}))
    with pytest.raises(SecretError):
        run(st.get(tenant="beta", name="sms_key"))

    # Copy the sealed blob under another NAME → open must fail under that name's AAD.
    run(st._s.put("operator_secrets", "acme|other",
                  {**row, "id": "acme|other", "name": "other"}))
    with pytest.raises(SecretError):
        run(st.get(tenant="acme", name="other"))


# ── nothing readable is stored or shown ───────────────────────────────────────
def test_the_plaintext_is_never_in_the_store():
    st = _store()
    run(st.set(tenant="acme", name="sms_key", value=SECRET))
    assert SECRET not in _raw(st)
    assert "sk-live" not in _raw(st)


def test_the_panel_view_has_no_value_field_at_all():
    st = _store()
    run(st.set(tenant="acme", name="sms_key", value=SECRET))
    info = run(st.info(tenant="acme", name="sms_key"))
    assert not hasattr(info, "value")
    assert SECRET not in repr(info)


def test_the_hint_identifies_the_key_without_revealing_it():
    st = _store()
    info = run(st.set(tenant="acme", name="sms_key", value=SECRET))
    assert info.hint.endswith("3f2a") and SECRET not in info.hint
    assert len(info.hint) <= 8


def test_a_short_credential_reveals_nothing_in_its_hint():
    st = _store()
    info = run(st.set(tenant="acme", name="pin", value="1234"))
    assert "1234" not in info.hint


def test_the_actor_is_recorded_as_a_pseudonym_not_an_identity():
    st = _store()
    info = run(st.set(tenant="acme", name="k", value=SECRET, actor_ref="sub_abc123"))
    assert info.set_by == "sub_abc123"
    assert "@" not in _raw(st), "an email reached the credential row"


# ── it still works for the adapter that needs it ──────────────────────────────
def test_an_adapter_can_read_the_real_value_in_process():
    st = _store()
    run(st.set(tenant="acme", name="sms_key", value=SECRET))
    assert run(st.get(tenant="acme", name="sms_key")) == SECRET


def test_config_remains_the_default_when_nothing_is_stored():
    # A deployment with no stored credential must behave exactly as it does today.
    st = _store()
    assert run(st.get(tenant="acme", name="sms_key", fallback="from-env")) == "from-env"


def test_a_stored_credential_overrides_config():
    st = _store()
    run(st.set(tenant="acme", name="sms_key", value=SECRET))
    assert run(st.get(tenant="acme", name="sms_key", fallback="from-env")) == SECRET


def test_a_tenant_without_an_override_inherits_the_platform_one():
    st = _store()
    run(st.set(tenant="", name="sms_key", value=SECRET))
    assert run(st.get(tenant="acme", name="sms_key")) == SECRET


def test_a_tenant_override_beats_the_platform_one():
    st = _store()
    run(st.set(tenant="", name="sms_key", value="platform-wide"))
    run(st.set(tenant="acme", name="sms_key", value="acme-only"))
    assert run(st.get(tenant="acme", name="sms_key")) == "acme-only"
    assert run(st.get(tenant="globex", name="sms_key")) == "platform-wide"


# ── rotation ──────────────────────────────────────────────────────────────────
def test_the_previous_credential_is_accepted_during_the_window():
    """A request in flight when a key changes must not fail, and a caller that has not yet been
    updated must not be locked out the instant somebody clicks rotate."""
    clock = {"t": 1000.0}
    st = _store(now=lambda: clock["t"], rotation_window_s=300)
    run(st.set(tenant="acme", name="webhook_secret", value="old-value"))
    run(st.set(tenant="acme", name="webhook_secret", value="new-value"))
    assert set(run(st.accepted(tenant="acme", name="webhook_secret"))) == {"old-value", "new-value"}


def test_the_previous_credential_stops_being_accepted_after_the_window():
    clock = {"t": 1000.0}
    st = _store(now=lambda: clock["t"], rotation_window_s=300)
    run(st.set(tenant="acme", name="webhook_secret", value="old-value"))
    run(st.set(tenant="acme", name="webhook_secret", value="new-value"))
    clock["t"] += 301
    assert run(st.accepted(tenant="acme", name="webhook_secret")) == ("new-value",)


def test_a_first_set_has_no_previous_and_is_not_rotating():
    st = _store()
    info = run(st.set(tenant="acme", name="k", value=SECRET))
    assert info.rotating is False


def test_a_rotation_is_visible_to_the_panel():
    st = _store()
    run(st.set(tenant="acme", name="k", value="one"))
    run(st.set(tenant="acme", name="k", value="two"))
    assert run(st.info(tenant="acme", name="k")).rotating is True


# ── refusals ──────────────────────────────────────────────────────────────────
def test_an_empty_value_is_refused_rather_than_stored():
    # "unset" and "set to nothing" must not be confusable.
    st = _store()
    with pytest.raises(SecretError, match="empty"):
        run(st.set(tenant="acme", name="k", value="   "))


def test_a_nameless_credential_is_refused():
    with pytest.raises(SecretError):
        run(_store().set(tenant="acme", name="", value=SECRET))


def test_clearing_removes_it_and_falls_back_to_config():
    st = _store()
    run(st.set(tenant="acme", name="k", value=SECRET))
    assert run(st.clear(tenant="acme", name="k")) is True
    assert run(st.get(tenant="acme", name="k", fallback="from-env")) == "from-env"


def test_info_for_an_unset_credential_is_none():
    assert run(_store().info(tenant="acme", name="never-set")) is None


def test_listing_shows_hints_and_never_values():
    st = _store()
    run(st.set(tenant="acme", name="a", value=SECRET))
    run(st.set(tenant="acme", name="b", value="another-secret-value"))
    infos = run(st.list_info(tenant="acme"))
    assert {i.name for i in infos} == {"a", "b"}
    assert SECRET not in repr(infos)
