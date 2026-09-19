"""TokenManager: caching, early refresh, SINGLE-FLIGHT, refresh-token rotation, revocation."""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.cache.memory import MemoryCache
from app.adapters.clock.manual import ManualClock
from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.provider.recording import RecordingConnector
from app.core import seal as _seal
from app.core.models import ACTIVE, NEEDS_REAUTH, Connection
from app.core.stores import ConnectionStore
from app.core.token_manager import TokenManager
from app.ports.connector_provider import CredentialInvalid, Tokens

SEAL = "k" * 32


def run(c):
    return asyncio.run(c)


def _env(provider=None, expires_in=3600, clock=None, access="A1"):
    st = MemoryDataStore()
    clk = clock or ManualClock(1000.0)
    prov = provider or RecordingConnector()
    conns = ConnectionStore(st)
    tm = TokenManager(connections=conns, provider=prov, clock=clk, seal_key=SEAL,
                      cache=MemoryCache(), cache_ttl=300, refresh_skew=60)
    conn = Connection(id="cn_1", tenant="t1", provider="rec")
    run(conns.save(conn))
    run(tm.store_tokens(conn, Tokens(access_token=access, refresh_token="R1", expires_in=expires_in),
                        invalidate=True))
    return tm, conns, prov, clk


def test_credentials_are_sealed_at_rest_and_hidden_from_api():
    # A DISTINCTIVE token, not the "A1" the other tests use. Sealing produces base64 over a random
    # nonce, so a 2-character needle turns up inside the ciphertext by pure chance about 1% of the
    # time — the assertion would fail on a clean codebase roughly once in a hundred runs and look
    # like a real leak. Long enough that a chance collision is impossible, so a failure here means
    # the credential genuinely was not sealed.
    secret = "ACCESS-TOKEN-THAT-MUST-NEVER-APPEAR-IN-PLAINTEXT"
    tm, conns, _, _ = _env(access=secret)
    stored = run(conns.get("cn_1"))
    assert secret not in stored.access_sealed and _seal.unseal(
        stored.access_sealed, SEAL, _seal.token_aad("cn_1", "access")) == secret
    assert "access_sealed" not in stored.public() and "refresh_sealed" not in stored.public()


def test_fresh_token_is_returned_without_refresh():
    tm, _, prov, _ = _env()
    assert run(tm.credentials("t1", "cn_1", {})) == "A1"
    assert prov.refresh_calls == 0


def test_expiring_token_refreshes_early_within_skew():
    tm, _, prov, clk = _env(expires_in=3600)
    clk.advance(3600 - 30)              # 30s left, skew is 60 → treated as expiring
    assert run(tm.credentials("t1", "cn_1", {})) == "rec_access_1"
    assert prov.refresh_calls == 1


def test_single_flight_n_concurrent_expiring_calls_do_exactly_one_refresh():
    tm, _, prov, clk = _env(expires_in=3600)
    clk.advance(3600 - 30)

    async def race():
        return await asyncio.gather(*[tm.credentials("t1", "cn_1", {}) for _ in range(20)])

    tokens = asyncio.run(race())
    assert prov.refresh_calls == 1, f"{prov.refresh_calls} refreshes — providers rotate; this bricks the connection"
    assert len(set(tokens)) == 1


def test_rotated_refresh_token_is_persisted():
    tm, conns, prov, clk = _env(expires_in=3600)
    clk.advance(3600)
    run(tm.credentials("t1", "cn_1", {}))
    conn = run(conns.get("cn_1"))
    assert _seal.unseal(conn.refresh_sealed, SEAL,
                        _seal.token_aad("cn_1", "refresh")) == "rec_refresh_1"   # not the old "R1"


def test_provider_that_does_not_rotate_keeps_the_existing_refresh_token():
    class NoRotate(RecordingConnector):
        def refresh(self, *, refresh_token, spec):
            self.refresh_calls += 1
            return Tokens(access_token="new_access", refresh_token="", expires_in=1800)

    tm, conns, _, clk = _env(provider=NoRotate(), expires_in=3600)
    clk.advance(3600)
    run(tm.credentials("t1", "cn_1", {}))
    conn = run(conns.get("cn_1"))
    assert _seal.unseal(conn.refresh_sealed, SEAL,
                        _seal.token_aad("cn_1", "refresh")) == "R1"    # never blanked


def test_cached_token_is_not_served_past_its_expiry():
    """The cache must not outlive the token — otherwise skew/refresh is pointless."""
    tm, _, prov, clk = _env(expires_in=3600)
    run(tm.credentials("t1", "cn_1", {}))     # warm the cache
    clk.advance(3600)                          # token now dead, cache TTL (300) irrelevant
    assert run(tm.credentials("t1", "cn_1", {})) == "rec_access_1"
    assert prov.refresh_calls == 1


def test_zero_expires_in_means_non_expiring_token():
    tm, _, prov, clk = _env(expires_in=0)
    clk.advance(10_000_000)
    assert run(tm.credentials("t1", "cn_1", {})) == "A1"
    assert prov.refresh_calls == 0


def test_invalid_grant_marks_connection_needs_reauth():
    class Dead(RecordingConnector):
        def refresh(self, *, refresh_token, spec):
            raise CredentialInvalid("invalid_grant")

    tm, conns, _, clk = _env(provider=Dead(), expires_in=3600)
    clk.advance(3600)
    with pytest.raises(CredentialInvalid):
        run(tm.credentials("t1", "cn_1", {}))
    assert run(conns.get("cn_1")).status == NEEDS_REAUTH


def test_another_replica_already_rotated_recovers_instead_of_bricking():
    """A cross-process race must not condemn a connection that now has a valid token.

    Replica B rotated the token first, so OUR refresh token is dead (invalid_grant) — but the
    stored connection already carries B's fresh access token. We must use it, not re-auth.
    """
    st = MemoryDataStore()
    clk = ManualClock(1000.0)
    real = ConnectionStore(st)

    class RotatedByPeer(ConnectionStore):
        """After the refresh attempt, `get()` reflects the peer's freshly rotated token."""

        def __init__(self):
            super().__init__(st)
            self.gets = 0

        async def get(self, cid):
            self.gets += 1
            conn = await super().get(cid)
            if conn and self.gets >= 3:            # the post-failure re-read
                conn.access_sealed = _seal.seal("token_from_other_replica", SEAL,
                                                _seal.token_aad("cn_1", "access"))
                conn.expires_at = clk.now() + 3600
            return conn

    class Racing(RecordingConnector):
        def refresh(self, *, refresh_token, spec):
            raise CredentialInvalid("invalid_grant")   # our refresh token was rotated away

    conns = RotatedByPeer()
    tm = TokenManager(connections=conns, provider=Racing(), clock=clk, seal_key=SEAL,
                      cache=MemoryCache(), cache_ttl=300, refresh_skew=60)
    conn = Connection(id="cn_1", tenant="t1", provider="rec")
    run(real.save(conn))
    run(tm.store_tokens(conn, Tokens(access_token="A1", refresh_token="R1", expires_in=3600),
                        invalidate=True))
    clk.advance(3600)
    assert run(tm.credentials("t1", "cn_1", {})) == "token_from_other_replica"
    assert run(real.get("cn_1")).status == ACTIVE       # NOT bricked into needs_reauth


def test_revoked_connection_cannot_resolve_credentials():
    tm, conns, _, _ = _env()
    conn = run(conns.get("cn_1"))
    conn.status = "revoked"
    run(conns.save(conn))
    run(tm.bump("t1"))
    with pytest.raises(CredentialInvalid):
        run(tm.credentials("t1", "cn_1", {}))


def test_bump_invalidates_the_cached_token_immediately():
    tm, conns, _, _ = _env()
    assert run(tm.credentials("t1", "cn_1", {})) == "A1"
    conn = run(conns.get("cn_1"))
    conn.access_sealed = _seal.seal("A2", SEAL, _seal.token_aad("cn_1", "access"))
    run(conns.save(conn))
    run(tm.bump("t1"))                                    # e.g. reconnect
    assert run(tm.credentials("t1", "cn_1", {})) == "A2"
