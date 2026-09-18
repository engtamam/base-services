"""Round-2 audit regressions (docs/audits/round2/identity-service.json).

Each test is RED on the pre-fix code and GREEN after:

  r2-01  the multi-tenant HUMAN mint strips '*'/'platform.*' from a customer-tenant membership,
         the same last door the single-tenant and service-account mints already held.
  r2-02  refresh rotation is atomic (CAS): two concurrent uses of ONE refresh token do not both
         mint a fresh family — exactly one rotation wins, the other is treated as reuse.
  r2-03  the last-owner invariant holds under concurrency: two concurrent owner removals can never
         strand the tenant with zero active owners.
  r2-04  the multi-tenant person login has a per-ACCOUNT lockout (across all source IPs), so
         distributed credential stuffing that stays under the per-source cap still trips a lock.
"""
from __future__ import annotations

import asyncio
import copy

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.login_identifier import UsernameIdentifier
from app.core.auth_service import AuthService, SoleOwnerError
from app.core.login_attempts import LoginAttemptStore
from app.core.membership import ACTIVE, Membership, MembershipStore, Person
from app.core.refresh_store import RefreshStore
from app.core.user_repository import UserRepository


def run(c):
    return asyncio.run(c)


class _YieldStore(MemoryDataStore):
    """Yields the loop inside every read/write, so gathered coroutines actually interleave on the
    same key — the interleaving the round-2 TOCTOU/CAS fixes have to survive."""

    async def get(self, c, k):
        await asyncio.sleep(0)
        return await super().get(c, k)

    async def find(self, c, where, **kw):
        await asyncio.sleep(0)
        return await super().find(c, where, **kw)

    async def delete(self, c, k):
        await asyncio.sleep(0)
        return await super().delete(c, k)

    async def cas(self, c, k, ver, val):
        await asyncio.sleep(0)
        return await super().cas(c, k, ver, val)


class _Hasher:
    driver = "plain"

    def hash(self, pw):
        return "h:" + pw

    def verify(self, pw, h):
        return h == "h:" + pw


class _CapturingIssuer:
    """Records the claims handed to issue_access so a test can inspect the minted token's caps."""

    driver = "i"

    def __init__(self):
        self.claims = []

    def issue_access(self, sub, claims, ttl):
        self.claims.append(claims)
        return f"access.{sub}"

    def issue_refresh(self, sub, jti, ttl):
        return f"refresh.{sub}.{jti}"


class _Verifier:
    driver = "v"

    def __init__(self):
        self.tokens = {}

    def verify(self, token):
        return self.tokens[token]


def _svc(store, *, issuer=None, verifier=None, platform_tenant="platform",
         lockout=None, account_lockout=None):
    return AuthService(
        users=UserRepository(store, UsernameIdentifier()), refresh=RefreshStore(store),
        hasher=_Hasher(), issuer=issuer or _CapturingIssuer(), verifier=verifier or _Verifier(),
        mfa=None, identifier=UsernameIdentifier(), access_ttl=900, refresh_ttl=3600,
        min_password_len=4, lockout=lockout, account_lockout=account_lockout,
        memberships=MembershipStore(store), platform_tenant=platform_tenant)


# ── r2-01: platform strip on the multi-tenant mint ─────────────────────────────
def test_customer_tenant_membership_never_mints_wildcard_or_platform_caps():
    store = MemoryDataStore()
    issuer = _CapturingIssuer()
    svc = _svc(store, issuer=issuer, platform_tenant="platform")
    person = Person(id="p1", identifier="sara@x.com", password_hash="h:x")
    # A customer-tenant membership that (however it got there) carries platform authority.
    m = Membership(person_id="p1", tenant="acme",
                   caps=["members:write", "*", "platform.admin"], roles=["owner"])
    run(svc.issue_tenant_tokens(person, m))
    caps = issuer.claims[-1]["caps"]
    assert "*" not in caps                      # the wildcard the gateway honours everywhere
    assert not any(c.startswith("platform.") for c in caps)
    assert "members:write" in caps              # a legitimate tenant cap survives


def test_platform_tenant_membership_keeps_platform_caps():
    store = MemoryDataStore()
    issuer = _CapturingIssuer()
    svc = _svc(store, issuer=issuer, platform_tenant="platform")
    person = Person(id="p1", identifier="ops@x.com", password_hash="h:x")
    m = Membership(person_id="p1", tenant="platform",
                   caps=["platform.support", "*"], roles=["owner"])
    run(svc.issue_tenant_tokens(person, m))
    caps = issuer.claims[-1]["caps"]
    assert "*" in caps and "platform.support" in caps   # the platform tenant is the one exception


def test_empty_platform_tenant_still_strips_on_multi_tenant_path():
    """r2-05 verification: the multi-tenant strip is UNCONDITIONAL — an empty PLATFORM_TENANT fails
    CLOSED (strips) rather than waving platform authority through as the single-tenant door does."""
    store = MemoryDataStore()
    issuer = _CapturingIssuer()
    svc = _svc(store, issuer=issuer, platform_tenant="")
    person = Person(id="p1", identifier="sara@x.com", password_hash="h:x")
    m = Membership(person_id="p1", tenant="acme", caps=["billing:read", "*"], roles=["member"])
    run(svc.issue_tenant_tokens(person, m))
    caps = issuer.claims[-1]["caps"]
    assert "*" not in caps and caps == ["billing:read"]


# ── r2-02: atomic refresh rotation ─────────────────────────────────────────────
def test_concurrent_refresh_of_one_token_lets_exactly_one_rotation_win():
    store = _YieldStore()
    v = _Verifier()
    svc = _svc(store, verifier=v)
    user = run(svc.register("t1", "alice", "pw-alice"))
    toks = run(svc.issue_tokens(user))
    other = run(svc.issue_tokens(user))          # a SECOND, independent session for the same user
    rt = toks["refresh_token"]
    jti = rt.split(".")[-1]
    v.tokens[rt] = {"type": "refresh", "jti": jti, "sub": user.id}
    ort = other["refresh_token"]
    v.tokens[ort] = {"type": "refresh", "jti": ort.split(".")[-1], "sub": user.id}

    async def _race():
        return await asyncio.gather(svc.refresh(rt), svc.refresh(rt))

    results = run(_race())
    minted = [r for r in results if r is not None]
    # Pre-fix: BOTH read 'active', both revoked, both minted a family (len == 2, reuse never seen).
    # Post-fix: the CAS lets exactly one rotation win; the loser mints nothing and nukes the family.
    assert len(minted) == 1
    # The consumed token is dead to any further use.
    assert run(svc.refresh(rt)) is None
    # revoke_family FIRED: the loser treated the concurrent second use as theft, so a DIFFERENT
    # live session for the same subject is now dead too (pre-fix it would keep working).
    assert run(svc.refresh(ort)) is None


def test_refresh_store_consume_is_single_winner():
    store = _YieldStore()
    rs = RefreshStore(store)
    run(rs.issue("j1", "u1", exp_ts=2 ** 40))

    async def _race():
        return await asyncio.gather(rs.consume("j1"), rs.consume("j1"), rs.consume("j1"))

    wins = [r for r in run(_race()) if r is True]
    assert len(wins) == 1                        # exactly one caller may rotate the jti


class _PgSemanticsStore(MemoryDataStore):
    """cas() with the POSTGRES adapter's exact semantics: expected_version=0 is a STRICT
    insert-if-absent (any existing row fails, versionless included); a versioned pin is an UPDATE.
    Guards the fix even if the memory adapter's cas were ever loosened again."""

    async def cas(self, c, k, ver, val):
        bucket = self._data.setdefault(c, {})
        current = bucket.get(k)
        if int(ver) == 0:
            if current is not None:
                return False
            stored = copy.deepcopy(val)
            stored["version"] = 1
            bucket[k] = stored
            return True
        cur_v = int(current.get("version", 0)) if current else 0
        if cur_v != int(ver):
            return False
        stored = copy.deepcopy(val)
        stored["version"] = cur_v + 1
        bucket[k] = stored
        return True


def _refresh_e2e_rotates(store):
    v = _Verifier()
    svc = _svc(store, verifier=v)
    user = run(svc.register("acme", "sara@x.com", "pw12"))
    rt = run(svc.issue_tokens(user))["refresh_token"]
    v.tokens[rt] = {"type": "refresh", "jti": rt.split(".")[-1], "sub": user.id}
    out = run(svc.refresh(rt))                    # THE call that 401'd for everyone in production
    assert out is not None and out["access_token"], "refresh must rotate, not 401"
    assert run(svc.refresh(rt)) is None          # the rotated token is now dead


def test_refresh_rotates_end_to_end_on_memory_store():
    """Regression: issue() wrote the refresh row with put() (no version) and consume() rotated it
    with a version-pinned cas. On postgres that cas is insert-if-absent and always failed on the
    existing row, so EVERY refresh returned None (401) while memory-backed unit tests passed —
    users were logged out each access-TTL. Now pinned on the (aligned) memory adapter."""
    _refresh_e2e_rotates(MemoryDataStore())


def test_refresh_rotates_end_to_end_under_postgres_cas_semantics():
    """The same flow against a store with the POSTGRES cas contract — the environment where the bug
    actually fired. This fails hard if issue() ever reverts to put()."""
    _refresh_e2e_rotates(_PgSemanticsStore())


# ── r2-03: last-owner invariant under concurrency ──────────────────────────────
def _seed_owner(ms, pid, tenant="acme"):
    run(ms.create_person(Person(id=pid, identifier=f"{pid}@x.com", password_hash="h:x")))
    run(ms.grant(Membership(person_id=pid, tenant=tenant, roles=["owner"], status=ACTIVE)))


def test_concurrent_removal_of_the_last_two_owners_never_strands_the_tenant():
    store = _YieldStore()
    ms = MembershipStore(store)
    svc = _svc(store)
    _seed_owner(ms, "owner1")
    _seed_owner(ms, "owner2")

    async def _race():
        return await asyncio.gather(
            svc.remove_member("acme", "owner1"),
            svc.remove_member("acme", "owner2"),
            return_exceptions=True)

    run(_race())
    # Pre-fix: each op saw the OTHER as an active owner and both revoked → zero owners left.
    # Post-fix: the commit-then-recheck-then-rollback guarantees at least one active owner survives.
    assert run(ms.has_other_active_owner("acme", "nobody")) is True


def test_concurrent_self_delete_and_admin_remove_never_strand_the_tenant():
    store = _YieldStore()
    ms = MembershipStore(store)
    svc = _svc(store)
    _seed_owner(ms, "ownerA")
    _seed_owner(ms, "ownerB")

    async def _race():
        return await asyncio.gather(
            svc.delete_self("ownerA", "acme"),
            svc.remove_member("acme", "ownerB"),
            return_exceptions=True)

    run(_race())
    assert run(ms.has_other_active_owner("acme", "nobody")) is True


# ── r2-04: per-account lockout on the multi-tenant person login ─────────────────
def test_distributed_stuffing_trips_the_account_lockout():
    store = MemoryDataStore()
    # Per-source cap set high so it NEVER trips (each IP makes one guess); the account counter is low.
    per_source = LoginAttemptStore(store, max_attempts=1000, window_seconds=900, lockout_seconds=900)
    account = LoginAttemptStore(store, max_attempts=3, window_seconds=3600, lockout_seconds=3600)
    svc = _svc(store, lockout=per_source, account_lockout=account)
    run(svc._memberships.create_person(
        Person(id="p1", identifier="victim@x.com", password_hash="h:correct", status=ACTIVE)))

    # Three failures, each from a DIFFERENT source IP — the per-source counter never fires.
    for i in range(3):
        assert run(svc.authenticate_person("victim@x.com", "guess", source=f"10.0.0.{i}")) is None
    # Pre-fix: no per-account counter, so the correct password from a fresh source succeeds.
    # Post-fix: the account is locked across ALL sources.
    assert run(svc.authenticate_person("victim@x.com", "correct", source="10.0.0.99")) is None


def test_account_lockout_does_not_break_a_clean_login():
    store = MemoryDataStore()
    per_source = LoginAttemptStore(store, max_attempts=5, window_seconds=900, lockout_seconds=900)
    account = LoginAttemptStore(store, max_attempts=50, window_seconds=3600, lockout_seconds=3600)
    svc = _svc(store, lockout=per_source, account_lockout=account)
    run(svc._memberships.create_person(
        Person(id="p1", identifier="ok@x.com", password_hash="h:correct", status=ACTIVE)))
    p = run(svc.authenticate_person("ok@x.com", "correct", source="10.0.0.1"))
    assert p is not None and p.id == "p1"
