"""Payment credentials an operator can change after launch.

This is the service where a wrong value moves money, so the tests that matter are the ones about
rotation: that a stored key reaches the gateway, that a callback signed with the PREVIOUS secret is
still honoured, and that a bad credential degrades to the configured one rather than failing every
charge.
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.credentials import CredentialResolver, Overlay
from app.core.secret_store import SecretStore

KEK = "unit-test-kek"


def run(c):
    return asyncio.run(c)


class _Settings:
    PAYMENT_API_KEY = "env-key"
    PAYMENT_EXTRA_SECRET = "env-extra"
    PAYMENT_WEBHOOK_SECRET = "env-webhook"
    ZATCA_SERVICE_TOKEN = "env-zatca"


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _store(now=None):
    return SecretStore(MemoryDataStore(), KEK, now=now or (lambda: 0.0))


# ── resolution ────────────────────────────────────────────────────────────────
def test_with_nothing_stored_the_environment_is_used():
    """An existing deployment must behave exactly as it did. Nothing has to be migrated."""
    got = run(CredentialResolver(_Settings(), _store()).resolve(""))
    assert got["PAYMENT_API_KEY"] == "env-key"


def test_a_stored_key_beats_the_environment():
    s = _store()
    run(s.set(tenant="", name="PAYMENT_API_KEY", value="stored-key"))
    assert run(CredentialResolver(_Settings(), s).resolve(""))["PAYMENT_API_KEY"] == "stored-key"


def test_a_tenant_keeps_its_own_gateway_account():
    s = _store()
    run(s.set(tenant="acme", name="PAYMENT_API_KEY", value="acme-key"))
    r = CredentialResolver(_Settings(), s)
    assert run(r.resolve("acme"))["PAYMENT_API_KEY"] == "acme-key"
    assert run(r.resolve("globex"))["PAYMENT_API_KEY"] == "env-key"


def test_an_undecryptable_credential_falls_back_instead_of_failing_every_charge():
    store = MemoryDataStore()
    run(SecretStore(store, KEK, now=lambda: 0.0).set(
        tenant="", name="PAYMENT_API_KEY", value="sealed-under-the-old-kek"))
    wrong = SecretStore(store, "a-different-kek", now=lambda: 0.0)
    assert run(CredentialResolver(_Settings(), wrong).resolve(""))["PAYMENT_API_KEY"] == "env-key"


# ── the refresh window ────────────────────────────────────────────────────────
def test_a_rotation_reaches_the_charge_path_after_the_window():
    """If the cache were a permanent capture this is the test that would fail, and the operator's
    change would silently never take effect."""
    s, clock = _store(), _Clock()
    r = CredentialResolver(_Settings(), s, ttl_s=30, now=clock)
    run(r.resolve(""))
    run(s.set(tenant="", name="PAYMENT_API_KEY", value="rotated"))
    clock.t = 31.0
    assert run(r.resolve(""))["PAYMENT_API_KEY"] == "rotated"


def test_within_the_window_the_credentials_are_not_read_again():
    s, clock = _store(), _Clock()
    r = CredentialResolver(_Settings(), s, ttl_s=30, now=clock)
    first = run(r.resolve(""))
    run(s.set(tenant="", name="PAYMENT_API_KEY", value="changed"))
    clock.t = 29.0
    assert run(r.resolve("")) is first


def test_invalidating_makes_a_change_visible_immediately():
    s = _store()
    r = CredentialResolver(_Settings(), s, ttl_s=3600)
    run(r.resolve(""))
    run(s.set(tenant="", name="PAYMENT_API_KEY", value="just-set-it"))
    r.invalidate("")
    assert run(r.resolve(""))["PAYMENT_API_KEY"] == "just-set-it"


# ── the gateway object ────────────────────────────────────────────────────────
def test_the_gateway_is_built_once_per_credential_set_not_per_request():
    """A gateway is an object. One rebuilt on every call loses whatever it holds between calls —
    which is how this first went wrong."""
    r = CredentialResolver(_Settings(), _store(), ttl_s=3600)
    builds = []

    def build(resolved, boot):
        builds.append(1)
        return object()

    a = run(r.gateway("", build, "boot"))
    b = run(r.gateway("", build, "boot"))
    assert a is b and len(builds) == 1


def test_a_rotation_produces_a_new_gateway():
    s = _store()
    r = CredentialResolver(_Settings(), s, ttl_s=3600)
    first = run(r.gateway("", lambda resolved, boot: object(), "boot"))
    run(s.set(tenant="", name="PAYMENT_API_KEY", value="rotated"))
    r.invalidate("")
    assert run(r.gateway("", lambda resolved, boot: object(), "boot")) is not first


# ── the webhook secret: the dangerous direction ───────────────────────────────
def test_a_callback_signed_with_the_previous_secret_is_still_honoured():
    """The gateway signed with whatever it held when it sent. Rejecting an in-flight callback as
    forged leaves an invoice unpaid after the customer's money has already moved."""
    clock = _Clock()
    s = SecretStore(MemoryDataStore(), KEK, now=lambda: clock.t, rotation_window_s=300)
    run(s.set(tenant="", name="PAYMENT_WEBHOOK_SECRET", value="old-secret"))
    run(s.set(tenant="", name="PAYMENT_WEBHOOK_SECRET", value="new-secret"))
    got = run(CredentialResolver(_Settings(), s).webhook_secrets(""))
    assert "new-secret" in got and "old-secret" in got


def test_after_the_rotation_window_the_old_secret_stops_being_accepted():
    """The overlap is bounded. A secret that is valid forever is not rotated, it is duplicated."""
    clock = _Clock()
    s = SecretStore(MemoryDataStore(), KEK, now=lambda: clock.t, rotation_window_s=300)
    run(s.set(tenant="", name="PAYMENT_WEBHOOK_SECRET", value="old-secret"))
    run(s.set(tenant="", name="PAYMENT_WEBHOOK_SECRET", value="new-secret"))
    clock.t = 301.0
    got = run(CredentialResolver(_Settings(), s).webhook_secrets(""))
    assert "old-secret" not in got and "new-secret" in got


def test_the_configured_secret_keeps_working_when_nothing_is_stored():
    got = run(CredentialResolver(_Settings(), _store()).webhook_secrets(""))
    assert got == ("env-webhook",)


# ── the overlay ───────────────────────────────────────────────────────────────
def test_the_overlay_replaces_only_what_was_resolved():
    o = Overlay(_Settings(), {"PAYMENT_API_KEY": "stored"})
    assert o.PAYMENT_API_KEY == "stored"
    assert o.PAYMENT_WEBHOOK_SECRET == "env-webhook"


def test_an_empty_resolved_value_does_not_blank_the_configured_one():
    # Otherwise clearing a credential in the panel would silently disable the gateway rather than
    # returning it to the value the deployment was configured with.
    o = Overlay(_Settings(), {"PAYMENT_API_KEY": ""})
    assert o.PAYMENT_API_KEY == "env-key"
