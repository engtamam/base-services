"""Regression tests for the atomic-cas fixes surfaced by the safety:atomic-cas gate.

Concurrency-safety of three get-then-put races that were converted to compare-and-set:
  • MembershipStore.create_person — global identifier uniqueness (T7: no two humans merged).
  • UserRepository.create — per-tenant username uniqueness (no duplicate accounts).
  • LoginAttemptStore.record_failure — the brute-force lockout counter (no lost increments).
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.login_attempts import LoginAttemptStore
from app.core.membership import MembershipStore, Person, PersonExists
from app.core.user_repository import UserExists, UserRepository
from app.core.models import User


def run(c):
    return asyncio.run(c)


class _YieldStore(MemoryDataStore):
    """Yields the loop inside get(), so gathered coroutines actually interleave on the same key."""
    async def get(self, c, k):
        await asyncio.sleep(0)
        return await super().get(c, k)


class _Ident:
    def normalize(self, raw: str) -> str:
        return raw.strip().lower()


def test_concurrent_create_person_one_wins():
    ms = MembershipStore(_YieldStore())

    async def _race():
        return await asyncio.gather(
            ms.create_person(Person(id="p1", identifier="ada@x.com", password_hash="h")),
            ms.create_person(Person(id="p2", identifier="ada@x.com", password_hash="h")),
            return_exceptions=True)

    results = run(_race())
    ok = [r for r in results if isinstance(r, Person)]
    dup = [r for r in results if isinstance(r, PersonExists)]
    assert len(ok) == 1 and len(dup) == 1          # one identity, one login (pre-fix: both created)


def test_concurrent_user_create_one_wins():
    repo = UserRepository(_YieldStore(), _Ident())

    async def _race():
        return await asyncio.gather(
            repo.create(User(id="u1", tenant="acme", username="Ada", password_hash="h")),
            repo.create(User(id="u2", tenant="acme", username="ada", password_hash="h")),
            return_exceptions=True)

    results = run(_race())
    ok = [r for r in results if isinstance(r, User)]
    dup = [r for r in results if isinstance(r, UserExists)]
    assert len(ok) == 1 and len(dup) == 1          # one account per normalized username


def test_concurrent_failures_all_count_toward_lockout():
    store = _YieldStore()
    la = LoginAttemptStore(store, max_attempts=5, window_seconds=900, lockout_seconds=900)

    async def _race():
        return await asyncio.gather(*[
            la.record_failure("acme", "ada", now=1000.0) for _ in range(5)])

    run(_race())
    assert run(la.is_locked("acme", "ada", now=1001.0)) is True   # all 5 counted (pre-fix: lost some)
