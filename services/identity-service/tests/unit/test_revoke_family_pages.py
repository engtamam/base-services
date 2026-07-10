"""`revoke_family` is the response to a detected refresh-token reuse. It must revoke ALL of them.

The old implementation read up to a million rows across every user and filtered in Python. A
`LIMIT` without an `ORDER BY` does not return the first N rows — it returns an arbitrary N. Past
the cap, "revoke every token for this user" revoked *some* of them, returned a count, and left
the attacker's stolen token working.

These tests would have passed against the old code at small N. That is the point: the bug only
appears past a boundary nobody tested, which is exactly where an attacker with a scripted client
puts you.
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.refresh_store import _PAGE, RefreshStore


def run(c):
    return asyncio.run(c)


class CountingStore(MemoryDataStore):
    def __init__(self):
        super().__init__()
        self.scans = 0

    async def scan_all(self, collection, limit):
        self.scans += 1
        return await super().scan_all(collection, limit)


def _seed(store: RefreshStore, user: str, n: int, start: int = 0) -> None:
    for i in range(start, start + n):
        run(store.issue(f"{user}-jti-{i:06d}", user, 9_999_999_999.0))


def test_revokes_every_token_past_a_single_page():
    st = CountingStore()
    store = RefreshStore(st)
    n = _PAGE * 2 + 7                       # straddles the page boundary, twice
    _seed(store, "victim", n)

    revoked = run(store.revoke_family("victim"))
    assert revoked == n, f"revoked {revoked} of {n} tokens and reported success"

    # And no token of that user survives — checked against the rows, not the return value.
    rows = run(st.scan_all("refresh_tokens", 10_000))
    survivors = [r for r in rows if r["user_id"] == "victim" and not r.get("revoked")]
    assert survivors == [], f"{len(survivors)} stolen tokens still valid after revoke_family"


def test_revocation_does_not_touch_another_users_tokens():
    st = CountingStore()
    store = RefreshStore(st)
    _seed(store, "victim", _PAGE + 3)
    _seed(store, "bystander", 20)

    run(store.revoke_family("victim"))

    rows = run(st.scan_all("refresh_tokens", 10_000))
    bystander = [r for r in rows if r["user_id"] == "bystander"]
    assert len(bystander) == 20
    assert all(not r.get("revoked") for r in bystander), "logged out an unrelated user"


def test_revocation_never_scans_the_token_table():
    st = CountingStore()
    store = RefreshStore(st)
    _seed(store, "victim", 5)
    st.scans = 0
    run(store.revoke_family("victim"))
    assert st.scans == 0, "revoke_family read every user's tokens"


def test_the_cost_is_flat_in_other_users_tokens():
    def rows_read(foreign: int) -> int:
        st = CountingStore()
        store = RefreshStore(st)
        _seed(store, "victim", 5)
        _seed(store, "other", foreign)
        seen = 0

        original = st.find

        async def counting_find(collection, where, **kw):
            nonlocal seen
            rows, nxt = await original(collection, where, **kw)
            seen += len(rows)
            return rows, nxt

        st.find = counting_find
        run(store.revoke_family("victim"))
        return seen

    assert rows_read(10) == rows_read(2_000) == 5


def test_revoking_twice_is_a_no_op_and_terminates():
    """The filter shrinks as rows are written. The keyset cursor is what keeps this finite."""
    st = CountingStore()
    store = RefreshStore(st)
    _seed(store, "victim", _PAGE + 1)

    assert run(store.revoke_family("victim")) == _PAGE + 1
    assert run(store.revoke_family("victim")) == 0
