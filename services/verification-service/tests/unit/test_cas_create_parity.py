"""Memory-adapter cas() parity with postgres (identity refresh-rotation bug class, safety:cas-create).

The postgres adapter's cas() is: expected_version=0 -> INSERT ... ON CONFLICT DO NOTHING (any
existing row, versionless included, makes it a no-op -> False); expected_version>=1 -> version-pinned
UPDATE. The memory adapter used to treat a versionless row's implicit version as 0 and let a cas(0)
"update" it — the divergence that let a version-pinned update pass every memory-backed test and
no-op in production. These tests pin the aligned semantics.
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.models import Verification
from app.core.stores import VerificationStore


def run(c):
    return asyncio.run(c)


def test_put_created_row_then_version_pinned_cas_succeeds():
    """The real create path: put() then cas_put() (version-pinned). The version rides in to_dict()
    at 1, so the first cas pins 1 and lands — exactly what postgres does. This is why the put create
    is a safety:cas-create false positive."""
    async def go():
        st = MemoryDataStore()
        store = VerificationStore(st)
        rec = Verification(id="v1", tenant="acme", subject="s")
        await store.put(rec)                       # put(): stores {"version": 1}
        row = await st.get("verifications", "v1")
        assert row["version"] == 1
        assert await store.cas_put(rec) is True    # cas(expected=1) matches the stored version
        assert rec.version == 2
        assert (await st.get("verifications", "v1"))["version"] == 2
    run(go())


def test_insert_if_absent_fails_on_existing_versionless_row():
    """cas(expected_version=0) must FAIL when any row already exists — versionless included —
    matching postgres INSERT ... ON CONFLICT DO NOTHING. The old lax adapter returned True here.
    This is the strictness identity_seen's once-per-identity claim relies on."""
    async def go():
        st = MemoryDataStore()
        await st.put("identity_seen", "k", {"id": "k"})     # versionless row (no version key)
        assert await st.cas("identity_seen", "k", 0, {"id": "k", "times": 99}) is False
        assert "times" not in (await st.get("identity_seen", "k"))
    run(go())


def test_insert_if_absent_succeeds_only_when_absent():
    async def go():
        st = MemoryDataStore()
        assert await st.cas("identity_seen", "k", 0, {"id": "k"}) is True
        assert (await st.get("identity_seen", "k"))["version"] == 1
        assert await st.cas("identity_seen", "k", 0, {"id": "k"}) is False   # present -> no-op
    run(go())
