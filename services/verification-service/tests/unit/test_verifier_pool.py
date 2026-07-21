"""Verifiers resolved from operator-managed credentials.

The interesting behaviour is staleness and failure: that a rotation actually reaches the check path
after the TTL, that the cache is a cache rather than a permanent capture, that one tenant's
credential never leaks into another's, and that an unreadable row does not take verification down.
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.secret_store import SecretStore
from app.core.verifier_pool import VerifierPool

KEK = "unit-test-kek"


def run(c):
    return asyncio.run(c)


class _Settings:
    """Only what the pool touches."""

    VERIFY_PROVIDERS = "business=wathq"
    verify_secrets = {"wathq": "WATHQ_TOKEN"}


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _pool(*, secrets=None, rc=None, ttl=30, clock=None, env="env-token"):
    """Verifiers are built as plain records so the test asserts on resolved values, not adapters."""
    return VerifierPool(
        _Settings(), secrets, rc,
        build=lambda spec, tokens: {"spec": spec, "token": tokens["WATHQ_TOKEN"]},
        fallback=lambda name: env,
        ttl_s=ttl, now=clock or _Clock())


def _store():
    return SecretStore(MemoryDataStore(), KEK, now=lambda: 0.0)


# ── the fallback ──────────────────────────────────────────────────────────────
def test_with_no_stored_credential_the_environment_is_used():
    """An existing deployment must behave exactly as it did. Nothing has to be migrated."""
    assert run(_pool(secrets=_store()).get(""))["token"] == "env-token"


def test_without_a_kek_the_environment_is_still_used():
    assert run(_pool(secrets=None).get(""))["token"] == "env-token"


# ── the rotation actually lands ───────────────────────────────────────────────
def test_a_rotation_reaches_the_check_path_after_the_ttl():
    """The whole point of the feature: a credential set in the panel is used, without a redeploy."""
    store, clock = _store(), _Clock()
    pool = _pool(secrets=store, ttl=30, clock=clock)
    assert run(pool.get(""))["token"] == "env-token"

    run(store.set(tenant="", name="WATHQ_TOKEN", value="rotated"))
    assert run(pool.get(""))["token"] == "env-token"      # within the window, still cached

    clock.t += 31
    assert run(pool.get(""))["token"] == "rotated"


def test_invalidate_makes_the_change_visible_immediately():
    """Set a key and test it — the common case must not be up to a TTL late on this replica."""
    store = _store()
    pool = _pool(secrets=store, ttl=3600)
    run(pool.get(""))
    run(store.set(tenant="", name="WATHQ_TOKEN", value="rotated"))
    pool.invalidate("")
    assert run(pool.get(""))["token"] == "rotated"


def test_one_tenants_credential_never_serves_another():
    store = _store()
    run(store.set(tenant="acme", name="WATHQ_TOKEN", value="acme-token"))
    pool = _pool(secrets=store)
    assert run(pool.get("acme"))["token"] == "acme-token"
    assert run(pool.get("other"))["token"] == "env-token"


# ── failure does not become an outage ─────────────────────────────────────────
def test_an_undecryptable_credential_falls_back_instead_of_raising():
    """One unreadable row must not stop the claim types whose providers are healthy."""
    ds = MemoryDataStore()
    run(SecretStore(ds, KEK).set(tenant="", name="WATHQ_TOKEN", value="rotated"))
    pool = _pool(secrets=SecretStore(ds, "a-different-kek"))
    assert run(pool.get(""))["token"] == "env-token"


# ── policy override ───────────────────────────────────────────────────────────
class _RC:
    def __init__(self, value):
        self.value = value

    async def get(self, *, tenant, key, default):
        return self.value if key == "VERIFY_PROVIDERS" else default


def test_an_operator_may_repoint_a_claim_type_at_another_provider():
    assert run(_pool(secrets=_store(), rc=_RC("business=other")).get(""))["spec"] == \
        "business=other"


def test_with_no_runtime_config_the_configured_providers_apply():
    assert run(_pool(secrets=_store()).get(""))["spec"] == "business=wathq"
