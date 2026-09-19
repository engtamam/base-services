"""HIGH regression: backup restore must rebuild the `signed::` chain head, or issuance wedges.

`signed::<tenant>` is the SOLE authority for the in-order signing gate (signed_head → _sign requires
icv == signed_icv+1). _rebuild_index derives the index from the restored invoices; it used to rebuild
only num:: and head::, leaving signed:: absent → signed_head=(0,"") → the next invoice waits forever
for ICV 1. It must now rebuild the CONTIGUOUSLY-signed head from the restored rows."""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.api.internal import _rebuild_index


def run(c):
    return asyncio.run(c)


def _put(st, iid, tenant, number, icv, ihash=None):
    row = {"id": iid, "tenant": tenant, "number": number, "icv": icv}
    if ihash is not None:
        row["hash"] = ihash        # a hash present ⇒ signed
    run(st.put("zatca_invoices", iid, row))


def test_rebuild_restores_the_contiguous_signed_head():
    st = MemoryDataStore()
    _put(st, "i1", "acme", "N1", 1, "h1")
    _put(st, "i2", "acme", "N2", 2, "h2")
    _put(st, "i3", "acme", "N3", 3, "h3")
    _put(st, "i4", "acme", "N4", 4)            # reserved but NOT signed (no hash)

    res = run(_rebuild_index(st, 10000))
    assert res["signed_heads"] == 1

    signed = run(st.get("zatca_index", "signed::acme"))
    assert signed == {"signed_icv": 3, "last_hash": "h3"}   # next invoice may sign ICV 4
    head = run(st.get("zatca_index", "head::acme"))
    assert head["reserved_icv"] == 4                        # no ICV missed
    assert run(st.get("zatca_index", "num::acme::N2"))["id"] == "i2"


def test_a_gap_stops_the_contiguous_head_below_the_top_signed():
    # signing is strictly in-order so this shouldn't occur, but the rebuild must be defensive: a gap
    # (ICV 2 unsigned) means the contiguous head is 1, not 3 — never claim signed past a hole.
    st = MemoryDataStore()
    _put(st, "i1", "acme", "N1", 1, "h1")
    _put(st, "i2", "acme", "N2", 2)            # gap: unsigned
    _put(st, "i3", "acme", "N3", 3, "h3")
    run(_rebuild_index(st, 10000))
    assert run(st.get("zatca_index", "signed::acme")) == {"signed_icv": 1, "last_hash": "h1"}


def test_no_signed_invoices_leaves_signed_head_absent():
    st = MemoryDataStore()
    _put(st, "i1", "acme", "N1", 1)            # reserved, never signed
    res = run(_rebuild_index(st, 10000))
    assert res["signed_heads"] == 0
    assert run(st.get("zatca_index", "signed::acme")) is None    # signed_head → (0,"") correctly
