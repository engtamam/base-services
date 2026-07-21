"""The signing key is resolved, not captured.

Capturing it at boot is exactly why rotating it used to need a redeploy. These tests pin the four
things that make the runtime path safe: the stored value WINS over the configured one, the
configured one is the fallback so an unmigrated deployment is untouched, the resolution is cached
off the hot path, and a write invalidates that cache.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.signed_url.hmac_signer import HmacSigner
from app.core.secret_store import SecretStore
from app.core.signer_pool import SIGNED_URL_SECRET, SignerPool

CONFIGURED = "configured-signing-key-0001"
STORED = "operator-entered-key-000002"
ROTATED = "rotated-key-00000000003"
TTL = 300


class _Clock:
    """Time is injected so the refresh window is asserted, not waited out."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _pool(store, clock, refresh=30):
    configured = HmacSigner(CONFIGURED, TTL)
    return SignerPool(secret_store=store, configured=configured,
                      build=lambda secret, also: HmacSigner(secret, TTL, also),
                      refresh_seconds=refresh, now=clock), configured


@pytest.fixture
def secret_store() -> SecretStore:
    return SecretStore(MemoryDataStore(), "unit-test-kek", rotation_window_s=300)


def test_with_nothing_stored_the_configured_signer_is_reused(secret_store):
    """An unmigrated deployment must behave EXACTLY as it does today — the same object, not an
    equivalent rebuild, so nothing has to be migrated first."""
    async def _body():
        pool, configured = _pool(secret_store, _Clock())
        assert await pool.get() is configured

    asyncio.run(_body())


def test_no_store_at_all_falls_back_to_configuration():
    """No KEK means no store. Share links must keep working, not start 403ing."""
    async def _body():
        pool, configured = _pool(None, _Clock())
        assert await pool.get() is configured

    asyncio.run(_body())


def test_a_stored_credential_wins_over_configuration(secret_store):
    """If configuration still won, the panel would report success and change nothing."""
    async def _body():
        await secret_store.set(tenant="", name=SIGNED_URL_SECRET, value=STORED)
        pool, _ = _pool(secret_store, _Clock())
        token = (await pool.get()).sign("f_1")
        assert HmacSigner(STORED, TTL).verify(token) is not None
        assert HmacSigner(CONFIGURED, TTL).verify(token) is None

    asyncio.run(_body())


def test_a_rotation_still_verifies_links_signed_with_the_previous_key(secret_store):
    """A link already in somebody's inbox cannot be recalled, and it carries its own expiry."""
    async def _body():
        await secret_store.set(tenant="", name=SIGNED_URL_SECRET, value=STORED)
        pool, _ = _pool(secret_store, _Clock())
        old_token = (await pool.get()).sign("f_1")

        await secret_store.set(tenant="", name=SIGNED_URL_SECRET, value=ROTATED)
        pool.invalidate()
        signer = await pool.get()
        assert signer.verify(old_token) is not None, "a live share link died on a rotation"
        # New links are minted under the CURRENT key only — nothing new is signed with a key that
        # is on its way out.
        assert HmacSigner(ROTATED, TTL).verify(signer.sign("f_2")) is not None

    asyncio.run(_body())


def test_the_resolution_is_cached_off_the_hot_path(secret_store):
    """Every share link and every public download touches the signer. A datastore read and a
    decrypt per request is not a cost this may pay for a change made a few times a year."""
    async def _body():
        await secret_store.set(tenant="", name=SIGNED_URL_SECRET, value=STORED)
        clock = _Clock()
        pool, _ = _pool(secret_store, clock, refresh=30)
        first = await pool.get()
        assert await pool.get() is first

        clock.t = 31
        assert await pool.get() is not first, "the cache never expired — a rotation never lands"

    asyncio.run(_body())


def test_a_write_invalidates_this_replica(secret_store):
    """So the common case — rotate the key, immediately test a link — is instant rather than up
    to a refresh window late."""
    async def _body():
        pool, configured = _pool(secret_store, _Clock())
        assert await pool.get() is configured
        await secret_store.set(tenant="", name=SIGNED_URL_SECRET, value=STORED)
        pool.invalidate()
        assert await pool.get() is not configured

    asyncio.run(_body())
