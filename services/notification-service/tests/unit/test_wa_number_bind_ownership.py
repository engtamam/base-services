"""WhatsAppNumberIndex.bind() is FIRST-WINS: a second tenant cannot silently steal a number's routing.

A duplicate/mistaken onboarding of one phone_number_id under a second tenant must NOT hijack inbound
routing (and delivery visibility) from the incumbent. bind() refuses the overwrite and reports a
conflict so the route can audit it, instead of the old unconditional put.
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.stores import WhatsAppNumberIndex


def run(c):
    return asyncio.run(c)


def test_first_bind_wins_and_a_different_tenant_is_refused():
    idx = WhatsAppNumberIndex(MemoryDataStore())
    assert run(idx.bind("PN1", "shop-A")) == WhatsAppNumberIndex.BOUND
    # A second tenant onboarding the SAME number is refused — the incumbent keeps the routing.
    assert run(idx.bind("PN1", "shop-B")) == WhatsAppNumberIndex.CONFLICT
    assert run(idx.tenant_of("PN1")) == "shop-A"          # NOT stolen


def test_same_tenant_rebind_is_idempotent():
    idx = WhatsAppNumberIndex(MemoryDataStore())
    run(idx.bind("PN1", "shop-A"))
    assert run(idx.bind("PN1", "shop-A")) == WhatsAppNumberIndex.UNCHANGED
    assert run(idx.tenant_of("PN1")) == "shop-A"


def test_empty_inputs_are_a_noop():
    idx = WhatsAppNumberIndex(MemoryDataStore())
    assert run(idx.bind("", "shop-A")) == WhatsAppNumberIndex.NOOP
    assert run(idx.bind("PN1", "")) == WhatsAppNumberIndex.NOOP
