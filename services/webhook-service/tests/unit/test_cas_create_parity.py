"""Memory-adapter cas() parity with postgres (identity refresh-rotation bug class, safety:cas-create).

The postgres adapter's cas() is: expected_version=0 -> INSERT ... ON CONFLICT DO NOTHING (any
existing row, versionless included, makes it a no-op -> False); expected_version>=1 -> version-pinned
UPDATE. The memory adapter used to treat a versionless row's implicit version as 0 and let a cas(0)
"update" it — the divergence that let a version-pinned claim pass every memory-backed test and
no-op in production. These tests pin the aligned semantics.
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.models import Delivery
from app.core.stores import DeliveryStore


def run(c):
    return asyncio.run(c)


def test_put_created_row_then_version_pinned_cas_succeeds():
    """The real create path: save() (put) then claim_delivering() (version-pinned). The version
    rides in to_dict() at 1, so the first claim pins 1 and lands — exactly what postgres does. This
    is why the put create is a safety:cas-create false positive."""
    async def go():
        st = MemoryDataStore()
        store = DeliveryStore(st)
        d = Delivery(id="d1", tenant="acme", event_id="e1", endpoint_id="ep1")
        await store.save(d)                        # put(): stores {"version": 1}
        assert (await st.get("deliveries", "d1"))["version"] == 1
        claimed = await store.claim_delivering(d)  # cas(expected=1) matches the stored version
        assert claimed is not None and claimed.version == 2
        assert (await st.get("deliveries", "d1"))["version"] == 2
    run(go())


def test_insert_if_absent_fails_on_existing_versionless_row():
    """cas(expected_version=0) must FAIL when any row already exists — versionless included —
    matching postgres INSERT ... ON CONFLICT DO NOTHING. The old lax adapter returned True here.
    This is the strictness InboundStore.reserve()'s dedupe claim relies on."""
    async def go():
        st = MemoryDataStore()
        await st.put("inbound", "k", {"id": "k"})           # versionless row (no version key)
        assert await st.cas("inbound", "k", 0, {"id": "k", "x": 1}) is False
        assert "x" not in (await st.get("inbound", "k"))
    run(go())


def test_insert_if_absent_succeeds_only_when_absent():
    async def go():
        st = MemoryDataStore()
        assert await st.cas("inbound", "k", 0, {"id": "k"}) is True
        assert (await st.get("inbound", "k"))["version"] == 1
        assert await st.cas("inbound", "k", 0, {"id": "k"}) is False   # present -> no-op
    run(go())
