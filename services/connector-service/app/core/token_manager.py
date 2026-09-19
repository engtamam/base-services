"""Credential/token resolution — the hot path — with SINGLE-FLIGHT refresh.

Every outbound provider call needs a valid access token, so this runs constantly:
  • the resolved token is **cached** (keyed by a per-tenant generation), so repeat calls do
    zero datastore reads; a `disconnect`/update bumps the generation and **revocation is
    immediate**;
  • we refresh **early** (`skew` seconds before expiry) because clocks disagree, and a token
    that expires mid-request is an avoidable failure;
  • refresh is **single-flight**: N concurrent callers on an expiring token perform exactly
    ONE refresh. This is not an optimisation — many providers **rotate** the refresh token,
    so two concurrent refreshes mean one caller ends up holding a dead token and the
    connection is permanently broken. The rotated token is persisted *before* it is used.
  • a provider that omits `refresh_token` on refresh (HubSpot) keeps its existing one — we
    never blank a credential we still need.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from app.core import seal as _seal
from app.core.models import ACTIVE, NEEDS_REAUTH, Connection
from app.core.stores import ConnectionStore
from app.ports.connector_provider import CredentialInvalid

# Process-wide, because the service object is rebuilt per request — a per-instance lock
# would give every caller its own lock and single-flight would silently do nothing.
# (Across replicas, the store re-check + the "someone else already rotated" recovery below
#  keep a cross-process race from bricking the connection.)
_LOCKS: dict[str, asyncio.Lock] = {}


def evict_lock(connection_id: str) -> None:
    """Drop a connection's refresh lock when it is disconnected. Without this the process-wide table
    grows one asyncio.Lock per connection id for the life of the process and never shrinks — an
    unbounded leak over a long-running service (connector-r2-07)."""
    _LOCKS.pop(connection_id, None)


class TokenManager:
    def __init__(self, *, connections: ConnectionStore, provider, clock, seal_key: str,
                 cache=None, cache_ttl: int = 60, refresh_skew: int = 60) -> None:
        self._conns = connections
        self._provider = provider
        self._clock = clock
        self._seal_key = seal_key
        self._cache = cache
        self._ttl = cache_ttl
        self._skew = refresh_skew

    # ── generation cache ───────────────────────────────────────────────────────
    async def _gen(self, tenant: str) -> str:
        if self._cache is None:
            return "0"
        try:
            return (await self._cache.get(f"conngen:{tenant}")) or "0"
        except Exception:
            return "0"

    async def bump(self, tenant: str) -> None:
        """Any credential/connection change → cached tokens are dead immediately."""
        if self._cache is None:
            return
        try:
            g = await self._cache.get(f"conngen:{tenant}")
            await self._cache.set(f"conngen:{tenant}", str(int(g or 0) + 1), None)
        except Exception:
            pass

    async def _cache_get(self, key: str):
        if self._cache is None:
            return None
        try:
            raw = await self._cache.get(key)
            return json.loads(raw) if raw else None
        except Exception:
            return None

    async def _cache_set(self, key: str, value) -> None:
        if self._cache is None:
            return
        try:
            await self._cache.set(key, json.dumps(value), self._ttl)
        except Exception:
            pass

    # ── the hot path ───────────────────────────────────────────────────────────
    def _expiring_at(self, expires_at: float) -> bool:
        if not expires_at:
            return False                                   # non-expiring token
        return self._clock.now() >= float(expires_at) - self._skew   # refresh EARLY

    def _expiring(self, conn: Connection) -> bool:
        return self._expiring_at(conn.expires_at)

    async def credentials(self, tenant: str, connection_id: str, spec: dict[str, Any]) -> str:
        """Return a valid access token. Refreshes (exactly once) if it is expiring."""
        gen = await self._gen(tenant)
        key = f"conntok:{gen}:{connection_id}"
        cached = await self._cache_get(key)
        # The cache must never outlive the token: a cached entry carries its own expiry, or
        # we would happily serve a dead token for the whole cache TTL.
        if cached is not None and not self._expiring_at(cached.get("expires_at", 0)):
            return cached["access_token"]

        conn = await self._conns.get(connection_id)
        if conn is None or conn.status != ACTIVE:
            raise CredentialInvalid("connection is not active")

        if not self._expiring(conn):
            token = _seal.unseal(conn.access_sealed, self._seal_key,
                                 _seal.token_aad(conn.id, "access")) or ""
            if not token:
                raise CredentialInvalid("no access token stored")
            await self._cache_set(key, {"access_token": token, "expires_at": conn.expires_at})
            return token

        return await self._refresh_single_flight(conn, spec, key)

    async def _refresh_single_flight(self, conn: Connection, spec: dict[str, Any], key: str) -> str:
        lock = _LOCKS.setdefault(conn.id, asyncio.Lock())
        async with lock:
            # Another coroutine refreshed while we waited on the lock — re-check, don't re-refresh.
            cached = await self._cache_get(key)
            if cached is not None and not self._expiring_at(cached.get("expires_at", 0)):
                return cached["access_token"]
            fresh = await self._conns.get(conn.id)
            if fresh is not None and not self._expiring(fresh):
                token = _seal.unseal(fresh.access_sealed, self._seal_key,
                                     _seal.token_aad(fresh.id, "access")) or ""
                await self._cache_set(key, {"access_token": token, "expires_at": fresh.expires_at})
                return token

            conn = fresh or conn
            refresh_token = _seal.unseal(conn.refresh_sealed, self._seal_key,
                                         _seal.token_aad(conn.id, "refresh")) or ""
            if not refresh_token:
                conn.status = NEEDS_REAUTH
                await self._conns.save(conn)
                raise CredentialInvalid("no refresh token; re-authorisation required")

            try:
                # OFFLOAD: refresh() does blocking urllib I/O to the OAuth token endpoint. Inline it
                # blocks the single-worker event loop for the whole round-trip AND the single-flight
                # lock holds it, so a slow token endpoint stalls the entire worker on every refresh.
                tokens = await asyncio.to_thread(
                    self._provider.refresh, refresh_token=refresh_token, spec=spec)
            except CredentialInvalid:
                # Another REPLICA may have rotated the token a moment ago, which invalidates
                # ours. Re-read before condemning the connection: if a fresh token now exists,
                # use it. Only a genuinely dead credential forces re-authorisation.
                latest = await self._conns.get(conn.id)
                if latest is not None and not self._expiring(latest) and latest.access_sealed:
                    token = _seal.unseal(latest.access_sealed, self._seal_key,
                                         _seal.token_aad(latest.id, "access")) or ""
                    if token:
                        await self._cache_set(key, {"access_token": token,
                                                    "expires_at": latest.expires_at})
                        return token
                conn.status = NEEDS_REAUTH          # terminal: stop retrying, ask the user
                await self._conns.save(conn)
                raise

            # Persist the ROTATED refresh token BEFORE the access token is used anywhere.
            await self.store_tokens(conn, tokens)
            await self._cache_set(key, {"access_token": tokens.access_token,
                                        "expires_at": conn.expires_at})
            return tokens.access_token

    async def store_tokens(self, conn: Connection, tokens, *, invalidate: bool = False) -> Connection:
        conn.access_sealed = _seal.seal(tokens.access_token, self._seal_key,
                                        _seal.token_aad(conn.id, "access"))
        if tokens.refresh_token:                    # rotated → store the new one
            conn.refresh_sealed = _seal.seal(tokens.refresh_token, self._seal_key,
                                             _seal.token_aad(conn.id, "refresh"))
        # else: the provider did not rotate (e.g. HubSpot) → keep the refresh token we have
        conn.expires_at = (self._clock.now() + tokens.expires_in) if tokens.expires_in else 0.0
        if tokens.scopes:
            conn.scopes = list(tokens.scopes)
        conn.status = ACTIVE
        await self._conns.save(conn)
        if invalidate:
            # Only connect/reconnect bumps: a refresh must NOT bump, or it would invalidate
            # the very cache entry it is about to write (and every other connection's).
            await self.bump(conn.tenant)
        return conn
