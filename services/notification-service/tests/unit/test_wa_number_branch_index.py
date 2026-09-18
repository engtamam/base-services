"""WhatsAppNumberIndex multi-branch support: a single shop (tenant) links MORE THAN ONE WhatsApp
number, each a physically separate branch with its own WABA + phone_number_id. waba_id/branch_id are
SEPARATE fields (never concatenated into a key), and by_waba_id resolves a WABA → its branch — the
lookup a Meta Connector event (keyed by WABA, not our tenant id) will need. First-wins/conflict
semantics are unchanged and still keyed on phone_number_id.
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.stores import WhatsAppNumberIndex


def run(c):
    return asyncio.run(c)


def test_two_branches_of_one_tenant_do_not_collide():
    idx = WhatsAppNumberIndex(MemoryDataStore())
    assert run(idx.bind("PN-jeddah", "shop-A", waba_id="WABA-J", branch_id="jeddah")) \
        == WhatsAppNumberIndex.BOUND
    assert run(idx.bind("PN-riyadh", "shop-A", waba_id="WABA-R", branch_id="riyadh")) \
        == WhatsAppNumberIndex.BOUND
    # both numbers route to the same tenant, independently
    assert run(idx.tenant_of("PN-jeddah")) == "shop-A"
    assert run(idx.tenant_of("PN-riyadh")) == "shop-A"


def test_by_waba_id_resolves_each_branch_to_its_own_row():
    idx = WhatsAppNumberIndex(MemoryDataStore())
    run(idx.bind("PN-jeddah", "shop-A", waba_id="WABA-J", branch_id="jeddah"))
    run(idx.bind("PN-riyadh", "shop-A", waba_id="WABA-R", branch_id="riyadh"))
    j = run(idx.by_waba_id("WABA-J"))
    r = run(idx.by_waba_id("WABA-R"))
    assert j == {"tenant": "shop-A", "branch_id": "jeddah", "phone_number_id": "PN-jeddah"}
    assert r == {"tenant": "shop-A", "branch_id": "riyadh", "phone_number_id": "PN-riyadh"}


def test_by_waba_id_unknown_returns_none():
    idx = WhatsAppNumberIndex(MemoryDataStore())
    assert run(idx.by_waba_id("nope")) is None
    assert run(idx.by_waba_id("")) is None


def test_branch_fields_are_separate_not_concatenated_into_a_key():
    store = MemoryDataStore()
    idx = WhatsAppNumberIndex(store)
    run(idx.bind("PN1", "shop-A", waba_id="WABA-1", branch_id="b1"))
    row = run(store.get("wa_number_index", "PN1"))
    # keyed by the bare phone_number_id; waba_id/branch_id live as their own fields
    assert row["id"] == "PN1" and row["phone_number_id"] == "PN1" and row["tenant"] == "shop-A"
    assert row["waba_id"] == "WABA-1" and row["branch_id"] == "b1"


def test_first_wins_still_holds_across_branches_of_different_tenants():
    idx = WhatsAppNumberIndex(MemoryDataStore())
    run(idx.bind("PN-shared", "shop-A", waba_id="WABA-A", branch_id="main"))
    # a DIFFERENT tenant claiming the same number is still refused, branch fields notwithstanding
    assert run(idx.bind("PN-shared", "shop-B", waba_id="WABA-B", branch_id="main")) \
        == WhatsAppNumberIndex.CONFLICT
    assert run(idx.by_waba_id("WABA-A"))["tenant"] == "shop-A"
    assert run(idx.by_waba_id("WABA-B")) is None          # never bound — the steal was refused


def test_rebind_without_branch_fields_preserves_existing_ones():
    idx = WhatsAppNumberIndex(MemoryDataStore())
    run(idx.bind("PN1", "shop-A", waba_id="WABA-1", branch_id="b1"))
    # a later same-tenant rebind that omits the branch fields must not wipe them
    assert run(idx.bind("PN1", "shop-A")) == WhatsAppNumberIndex.UNCHANGED
    assert run(idx.by_waba_id("WABA-1")) == {"tenant": "shop-A", "branch_id": "b1",
                                             "phone_number_id": "PN1"}
