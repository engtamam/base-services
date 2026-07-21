"""Provider chains resolved from operator-managed credentials.

The interesting behaviour is all about staleness and failure: that a rotation actually reaches the
send path, that the cache is a cache rather than a permanent capture, and that one unusable
provider does not take the whole channel down.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.provider_pool import ProviderPool
from app.core.secret_store import SecretStore

KEK = "unit-test-kek"


def run(c):
    return asyncio.run(c)


class _Settings:
    """Only what the pool touches."""

    RESEND_API_KEY = "env-resend"
    TAQNYAT_TOKEN = "env-taqnyat"
    email_chain = ["resend"]
    sms_chain = ["taqnyat"]

    def __getattr__(self, name):     # every other credential is unset in the environment
        return ""


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _pool(*, secrets=None, rc=None, ttl=30, clock=None):
    """Chains are built as plain records so the test asserts on resolved values, not on adapters."""
    return ProviderPool(
        _Settings(), secrets, rc,
        build_email=lambda chain, creds: {"chain": list(chain), "key": creds["RESEND_API_KEY"]},
        build_sms=lambda chain, creds: {"chain": list(chain), "key": creds["TAQNYAT_TOKEN"]},
        ttl_s=ttl, now=clock or _Clock())


def _store():
    return SecretStore(MemoryDataStore(), KEK, now=lambda: 0.0)


# ── the fallback ──────────────────────────────────────────────────────────────
def test_with_no_stored_credential_the_environment_is_used():
    """An existing deployment must behave exactly as it did. Nothing has to be migrated."""
    email, sms = run(_pool(secrets=_store()).get(""))
    assert email["key"] == "env-resend"
    assert sms["key"] == "env-taqnyat"


def test_without_a_secret_store_at_all_the_environment_is_used():
    email, _ = run(_pool(secrets=None).get(""))
    assert email["key"] == "env-resend"


# ── the override ──────────────────────────────────────────────────────────────
def test_a_stored_credential_beats_the_environment():
    s = _store()
    run(s.set(tenant="", name="RESEND_API_KEY", value="stored-resend"))
    email, _ = run(_pool(secrets=s).get(""))
    assert email["key"] == "stored-resend"


def test_a_tenant_inherits_the_platform_credential():
    s = _store()
    run(s.set(tenant="", name="RESEND_API_KEY", value="platform-key"))
    email, _ = run(_pool(secrets=s).get("acme"))
    assert email["key"] == "platform-key"


def test_a_tenants_own_credential_beats_the_platform_one():
    s = _store()
    run(s.set(tenant="", name="RESEND_API_KEY", value="platform-key"))
    run(s.set(tenant="acme", name="RESEND_API_KEY", value="acme-key"))
    assert run(_pool(secrets=s).get("acme"))[0]["key"] == "acme-key"
    assert run(_pool(secrets=s).get("globex"))[0]["key"] == "platform-key"


def test_tenants_do_not_see_each_others_chains():
    s = _store()
    run(s.set(tenant="acme", name="RESEND_API_KEY", value="acme-key"))
    run(s.set(tenant="globex", name="RESEND_API_KEY", value="globex-key"))
    pool = _pool(secrets=s)
    assert run(pool.get("acme"))[0]["key"] == "acme-key"
    assert run(pool.get("globex"))[0]["key"] == "globex-key"


# ── the cache, which is the whole hot-path design ─────────────────────────────
def test_within_the_window_the_chain_is_not_rebuilt():
    """Fourteen credentials resolved per send would be fourteen reads and fourteen decrypts."""
    s = _store()
    clock = _Clock()
    pool = _pool(secrets=s, ttl=30, clock=clock)
    first = run(pool.get(""))[0]
    run(s.set(tenant="", name="RESEND_API_KEY", value="changed-mid-window"))
    clock.t = 29.0
    assert run(pool.get(""))[0] is first, "the chain was rebuilt inside the refresh window"


def test_after_the_window_a_rotation_reaches_the_send_path():
    """The cache is a cache. If it were a permanent capture this is the test that would fail, and
    the operator's change would silently never take effect."""
    s = _store()
    clock = _Clock()
    pool = _pool(secrets=s, ttl=30, clock=clock)
    run(pool.get(""))
    run(s.set(tenant="", name="RESEND_API_KEY", value="rotated"))
    clock.t = 31.0
    assert run(pool.get(""))[0]["key"] == "rotated"


def test_invalidating_makes_a_change_visible_immediately():
    s = _store()
    clock = _Clock()
    pool = _pool(secrets=s, ttl=3600, clock=clock)
    run(pool.get(""))
    run(s.set(tenant="", name="RESEND_API_KEY", value="just-set-it"))
    pool.invalidate("")
    assert run(pool.get(""))[0]["key"] == "just-set-it"


def test_invalidating_one_tenant_leaves_the_others_cached():
    s = _store()
    pool = _pool(secrets=s, ttl=3600)
    run(pool.get("acme"))
    other = run(pool.get("globex"))[0]
    pool.invalidate("acme")
    assert run(pool.get("globex"))[0] is other


# ── failure does not become an outage ─────────────────────────────────────────
def test_an_undecryptable_credential_falls_back_instead_of_raising():
    """A KEK that no longer matches must not make every send raise. The environment value is a
    working answer, and one bad row is an operator problem rather than a total outage."""
    store = MemoryDataStore()
    run(SecretStore(store, KEK, now=lambda: 0.0).set(
        tenant="", name="RESEND_API_KEY", value="sealed-under-the-old-kek"))
    wrong = SecretStore(store, "a-different-kek", now=lambda: 0.0)
    email, _ = run(_pool(secrets=wrong).get(""))
    assert email["key"] == "env-resend"


# ── the operator may reorder providers ────────────────────────────────────────
class _Rc:
    def __init__(self, values: dict) -> None:
        self._v = values

    async def get(self, *, tenant, key, default):
        return self._v.get(key, default)


def test_the_provider_order_can_be_changed_at_runtime():
    email, _ = run(_pool(secrets=_store(), rc=_Rc({"EMAIL_CHAIN": "postmark,resend"})).get(""))
    assert email["chain"] == ["postmark", "resend"]


def test_an_empty_override_falls_back_to_the_configured_order():
    # An operator clearing the field must not leave the service with no providers at all.
    email, _ = run(_pool(secrets=_store(), rc=_Rc({"EMAIL_CHAIN": "  ,  "})).get(""))
    assert email["chain"] == ["resend"]


@pytest.mark.parametrize("chain", ["", None])
def test_no_override_uses_the_configured_order(chain):
    email, _ = run(_pool(secrets=_store(), rc=_Rc({})).get(""))
    assert email["chain"] == ["resend"]
