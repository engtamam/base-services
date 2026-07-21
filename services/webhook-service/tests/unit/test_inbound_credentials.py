"""Inbound verification secrets are resolved, not captured.

Capturing the packed INBOUND_SECRETS string at boot is exactly why onboarding a source, or
accepting a provider's rotation, used to need a redeploy. These tests pin what makes the runtime
path safe: the stored secret WINS, configuration remains the fallback for an unmigrated deployment,
a rotation keeps accepting the previous value, an unreadable row fails CLOSED, and the resolution is
cached off the hot path.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.inbound_credentials import InboundSecretPool, secret_name
from app.core.secret_store import SecretStore

CONFIGURED = "configured-stripe-secret"
STORED = "whsec_operator_entered"
ROTATED = "whsec_rotated"


class _Clock:
    """Time is injected so the refresh window is asserted, not waited out."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _pool(store, clock, refresh=30, configured=None):
    return InboundSecretPool(
        secret_store=store,
        configured={"stripe": [CONFIGURED]} if configured is None else configured,
        refresh_seconds=refresh, now=clock)


@pytest.fixture
def datastore() -> MemoryDataStore:
    return MemoryDataStore()


@pytest.fixture
def secret_store(datastore) -> SecretStore:
    return SecretStore(datastore, "unit-test-kek", rotation_window_s=300)


def test_with_nothing_stored_configuration_still_verifies(secret_store):
    """An unmigrated deployment must behave EXACTLY as it does today — nothing has to be moved
    first, and a source that verified yesterday still verifies."""
    async def _body():
        assert await _pool(secret_store, _Clock()).get() == {"stripe": [CONFIGURED]}

    asyncio.run(_body())


def test_no_store_at_all_falls_back_to_configuration():
    """No KEK means no store. Inbound events must keep being accepted, not start 400ing."""
    async def _body():
        assert await _pool(None, _Clock()).get() == {"stripe": [CONFIGURED]}

    asyncio.run(_body())


def test_a_stored_secret_replaces_the_configured_one(secret_store):
    """It REPLACES rather than adds: keeping both would mean a leaked secret can never be retired
    without the redeploy this exists to avoid. And if configuration still won, the panel would
    report success and change nothing."""
    async def _body():
        await secret_store.set(tenant="", name=secret_name("stripe"), value=STORED)
        assert await _pool(secret_store, _Clock()).get() == {"stripe": [STORED]}

    asyncio.run(_body())


def test_a_new_source_can_be_onboarded_without_a_redeploy(secret_store):
    async def _body():
        await secret_store.set(tenant="", name=secret_name("github"), value="gh-secret")
        got = await _pool(secret_store, _Clock()).get()
        assert got["github"] == ["gh-secret"]
        assert got["stripe"] == [CONFIGURED], "an unrelated source lost its secret"

    asyncio.run(_body())


def test_a_rotation_still_accepts_the_previous_secret(secret_store):
    """The provider signs with whatever it held when it sent. A callback in flight has no version
    to negotiate, and rejecting it looks exactly like a forgery."""
    async def _body():
        await secret_store.set(tenant="", name=secret_name("stripe"), value=STORED)
        await secret_store.set(tenant="", name=secret_name("stripe"), value=ROTATED)
        pool = _pool(secret_store, _Clock())
        accepted = (await pool.get())["stripe"]
        assert accepted[0] == ROTATED
        assert STORED in accepted, "a callback already in flight would be rejected as forged"

    asyncio.run(_body())


def test_an_unreadable_secret_fails_closed(datastore):
    """A row sealed under a KEK we no longer hold must not leave the configured value quietly in
    place: that would WIDEN trust to a secret the operator believes they replaced."""
    async def _body():
        await SecretStore(datastore, "the-original-kek").set(
            tenant="", name=secret_name("stripe"), value=STORED)
        wrong = SecretStore(datastore, "a-different-kek")
        assert await _pool(wrong, _Clock()).get() == {}

    asyncio.run(_body())


def test_the_resolution_is_cached_off_the_hot_path(secret_store):
    """Every third-party event lands here. A datastore read and a decrypt per callback is not a
    cost this may pay for a change made a few times a year."""
    async def _body():
        clock = _Clock()
        pool = _pool(secret_store, clock, refresh=30)
        assert await pool.get() == {"stripe": [CONFIGURED]}
        await secret_store.set(tenant="", name=secret_name("stripe"), value=STORED)
        assert await pool.get() == {"stripe": [CONFIGURED]}, "the store was read per request"

        clock.t = 31
        assert await pool.get() == {"stripe": [STORED]}, "the cache never expired"

    asyncio.run(_body())


def test_a_write_invalidates_this_replica(secret_store):
    """So the common case — enter a secret, immediately replay the provider's test event — is
    instant rather than up to a refresh window late."""
    async def _body():
        pool = _pool(secret_store, _Clock())
        assert await pool.get() == {"stripe": [CONFIGURED]}
        await secret_store.set(tenant="", name=secret_name("stripe"), value=STORED)
        pool.invalidate()
        assert await pool.get() == {"stripe": [STORED]}

    asyncio.run(_body())
