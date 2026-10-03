"""Non-security MEDIUM remediation (identity-service):
  008  JWKS verifier bounds forced refreshes — a flood of distinct bogus kids cannot amplify into one
       upstream fetch each; a repeat bogus kid is negative-cached.
  011  the paged owner walk RAISES when it hits its page bound with rows unread, instead of returning a
       possibly-wrong False/None on a decision path.
  012  /otp/verify is scoped to the 'verify' purpose only (login/reset codes are not spent there).
"""
from __future__ import annotations

import asyncio

import pytest


def run(c):
    return asyncio.run(c)


# ── 008: bounded JWKS refetch on unknown kids ─────────────────────────────────
def test_jwks_debounces_forced_refresh_across_distinct_bogus_kids(monkeypatch):
    import jwt

    from app.adapters.token_verifier.jwks import JWKSVerifier

    v = JWKSVerifier("https://issuer.example/jwks", cache_lifespan_s=300)

    refreshes = {"n": 0}

    class _Client:
        def get_signing_key(self, kid):
            raise jwt.PyJWKClientError("no such kid")

        def get_jwk_set(self, refresh=False):
            refreshes["n"] += 1

    v._client = _Client()

    def _hdr(kid):
        monkeypatch.setattr(jwt, "get_unverified_header", lambda t: {"kid": kid})

    from app.ports.errors import InvalidToken

    # Ten DISTINCT bogus kids must trigger at most ONE upstream refresh (the debounce), not ten.
    for i in range(10):
        _hdr(f"bogus-{i}")
        with pytest.raises(InvalidToken):
            v._signing_key("tok")
    assert refreshes["n"] == 1


def test_jwks_negative_caches_a_repeated_bogus_kid(monkeypatch):
    import jwt

    from app.adapters.token_verifier.jwks import JWKSVerifier
    from app.ports.errors import InvalidToken

    v = JWKSVerifier("https://issuer.example/jwks", cache_lifespan_s=300)
    refreshes = {"n": 0}

    class _Client:
        def get_signing_key(self, kid):
            raise jwt.PyJWKClientError("no such kid")

        def get_jwk_set(self, refresh=False):
            refreshes["n"] += 1

    v._client = _Client()
    monkeypatch.setattr(jwt, "get_unverified_header", lambda t: {"kid": "same-bogus"})
    for _ in range(5):
        with pytest.raises(InvalidToken):
            v._signing_key("tok")
    assert refreshes["n"] == 1          # refreshed once, then negative-cached


# ── 011: the owner walk refuses to guess past its page bound ──────────────────
class _PagingStore:
    """A store whose find() always returns a full page and a non-None cursor — i.e. 'more remains'."""
    def __init__(self, row):
        self._row = row

    async def find(self, coll, where, *, limit, after, sort):
        return [dict(self._row) for _ in range(limit)], "more"


def test_has_other_active_owner_raises_when_page_bound_exceeded():
    from app.core.membership import MembershipStore, MembershipWalkIncomplete

    # every row is a NON-matching membership (same person, so never "another owner"), and the cursor
    # never ends → the walk exhausts max_pages with rows still unread.
    store = MembershipStore.__new__(MembershipStore)
    store._store = _PagingStore({"person_id": "p1", "tenant": "t", "roles": ["owner"],
                                 "status": "active"})
    with pytest.raises(MembershipWalkIncomplete):
        run(store.has_other_active_owner("t", "p1", page=10, max_pages=3))


def test_founding_owner_raises_when_page_bound_exceeded():
    from app.core.membership import MembershipStore, MembershipWalkIncomplete

    store = MembershipStore.__new__(MembershipStore)
    store._store = _PagingStore({"person_id": "p1", "tenant": "t", "roles": ["staff"],
                                 "status": "active"})
    with pytest.raises(MembershipWalkIncomplete):
        run(store.founding_owner("t", page=10, max_pages=3))
