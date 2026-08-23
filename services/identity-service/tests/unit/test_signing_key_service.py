"""The runtime keyring service: seeds from a PEM, seals to the store, serves a JWKS, and rotates
idempotently under a repeated cron (min-age guard) while a second replica picks the new key up."""
from __future__ import annotations

import asyncio

import pytest

from app.core.signing_key_service import KEYRING_NAME, SigningKeyService
from app.core.signing_keyring import generate_rsa_keypair

_PRIV, _ = generate_rsa_keypair()


class FakeSecretStore:
    """Stands in for SecretStore.get/set — a shared dict is two replicas over one datastore."""

    def __init__(self, backing: dict | None = None) -> None:
        self._d = backing if backing is not None else {}

    async def get(self, *, tenant: str, name: str, fallback: str = "") -> str:
        return self._d.get((tenant, name), fallback)

    async def set(self, *, tenant: str, name: str, value: str, actor_ref: str = "") -> None:
        self._d[(tenant, name)] = value


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_seed_persists_and_serves_a_jwks():
    async def go():
        store = FakeSecretStore()
        svc = SigningKeyService(store, overlap_seconds=600, seed_pem=_PRIV, now=Clock())
        await svc.load()
        assert (("", KEYRING_NAME) in store._d)              # sealed row was written
        kid, priv = svc.current_signing_key()
        assert priv.strip().startswith("-----BEGIN")
        assert [k["kid"] for k in svc.jwks()["keys"]] == [kid]
    asyncio.run(go())


def test_rotation_is_a_noop_before_min_age_and_fires_after():
    async def go():
        clk = Clock(1000.0)
        svc = SigningKeyService(FakeSecretStore(), overlap_seconds=600, seed_pem=_PRIV,
                                min_age_seconds=3600, now=clk)
        await svc.load()
        first_kid, _ = svc.current_signing_key()
        r1 = await svc.rotate()                             # current key is brand new → skipped
        assert r1["rotated"] is False and svc.current_signing_key()[0] == first_kid
        clk.t = 1000.0 + 3601                               # now past min age
        r2 = await svc.rotate()
        assert r2["rotated"] is True and svc.current_signing_key()[0] != first_kid
        assert len(svc.jwks()["keys"]) == 2                 # both keys advertised during overlap
    asyncio.run(go())


def test_force_rotates_even_when_the_key_is_young():
    async def go():
        svc = SigningKeyService(FakeSecretStore(), overlap_seconds=600, seed_pem=_PRIV,
                                min_age_seconds=3600, now=Clock())
        await svc.load()
        first = svc.current_signing_key()[0]
        r = await svc.rotate(force=True)
        assert r["rotated"] is True and r["reason"] == "forced"
        assert svc.current_signing_key()[0] != first
    asyncio.run(go())


def test_a_second_replica_picks_up_the_rotation_via_refresh():
    async def go():
        backing: dict = {}
        clk = Clock(1000.0)
        a = SigningKeyService(FakeSecretStore(backing), overlap_seconds=600, seed_pem=_PRIV,
                              min_age_seconds=3600, now=clk)
        b = SigningKeyService(FakeSecretStore(backing), overlap_seconds=600, seed_pem=_PRIV, now=clk)
        await a.load()
        await b.load()
        clk.t += 3601
        rotated = await a.rotate()                          # replica A rotates
        assert rotated["rotated"] is True
        assert b.current_signing_key()[0] != a.current_signing_key()[0]   # B still on old key...
        await b.refresh()
        assert b.current_signing_key()[0] == a.current_signing_key()[0]   # ...until it refreshes
    asyncio.run(go())


def test_load_without_seed_or_stored_ring_refuses():
    async def go():
        svc = SigningKeyService(FakeSecretStore(), overlap_seconds=600, seed_pem="", now=Clock())
        with pytest.raises(ValueError):
            await svc.load()
    asyncio.run(go())


def test_lapsed_keys_are_pruned_on_load():
    async def go():
        backing: dict = {}
        clk = Clock(1000.0)
        a = SigningKeyService(FakeSecretStore(backing), overlap_seconds=600, seed_pem=_PRIV,
                              min_age_seconds=1, now=clk)
        await a.load()
        clk.t += 10
        await a.rotate()                                    # ring now has 2 keys, old retires at +600
        clk.t += 10_000                                     # long past the overlap
        fresh = SigningKeyService(FakeSecretStore(backing), overlap_seconds=600, seed_pem=_PRIV,
                                  now=clk)
        await fresh.load()                                  # load prunes the lapsed key
        assert len(fresh.jwks()["keys"]) == 1
    asyncio.run(go())
