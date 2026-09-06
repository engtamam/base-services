"""A ZATCA tax invoice is sealed at issuance and is NEVER altered — not on a DSAR, not ever.

Tax law says keep the invoice (6–15 years); ZATCA seals it cryptographically so its details — the
buyer's name included (a mandatory field) — cannot change after issuance without breaking the
signature. Privacy law (PDPL) exempts data another law requires kept. So the invoice is retained IN
FULL and UNALTERED indefinitely; the buyer's name is never pseudonymised. Deleting it is illegal;
editing it is illegal too.
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.erasure import BillingErasure
from app.core.pii import subject_ref

SALT = "product-pii-salt"
TENANT, SUBJECT = "t1", "alice@x.com"
REF = subject_ref(SALT, TENANT, SUBJECT)


def run(c):
    return asyncio.run(c)


def _eraser(store):
    return BillingErasure(store=store, pii_salt=SALT)


def _invoice(store, **over):
    inv = {"id": over.get("id", "inv_1"), "tenant": TENANT, "buyer_name": "Alice Al-Otaibi",
           "buyer_vat": "", "buyer_cr": "", "total_minor": 115_00, "vat_minor": 15_00,
           "created": over.get("created", "2025-06-01T00:00:00+00:00")}
    inv.update(over)
    run(store.put("invoices", inv["id"], inv))
    return inv


# ── the invoice is NEVER altered, at any age ────────────────────────────────────
def test_a_recent_invoice_is_left_unaltered():
    store = MemoryDataStore()
    _invoice(store, created="2025-06-01T00:00:00+00:00")
    run(_eraser(store).erase(TENANT, SUBJECT))
    kept = run(store.get("invoices", "inv_1"))
    assert kept["buyer_name"] == "Alice Al-Otaibi"
    assert kept["total_minor"] == 115_00 and kept["vat_minor"] == 15_00


def test_even_a_decades_old_invoice_is_never_pseudonymised():
    """The old behaviour pseudonymised the name once retention lapsed — that ALTERS a sealed ZATCA
    invoice, which is forbidden. A 16-year-old invoice keeps its buyer_name exactly as issued."""
    store = MemoryDataStore()
    _invoice(store, created="2005-01-01T00:00:00+00:00")
    run(_eraser(store).erase(TENANT, SUBJECT))
    kept = run(store.get("invoices", "inv_1"))
    assert kept["buyer_name"] == "Alice Al-Otaibi" and kept["buyer_name"] != REF


def test_ack_reports_the_record_retained_unaltered_with_a_basis():
    store = MemoryDataStore()
    _invoice(store)
    ack = run(_eraser(store).erase(TENANT, SUBJECT))
    assert ack["erased"] == []
    assert ack["already_absent"] is False              # we found the person's data; we retained it
    entry = next(r for r in ack["retained"] if r["what"].startswith("invoices:"))
    assert entry["basis"] == "tax_retention"
    why = entry["why"].lower()
    assert "unaltered" in why and "name" in why and "indefinitely" in why


def test_erasure_is_a_noop_and_stays_found_on_retry():
    store = MemoryDataStore()
    _invoice(store)
    first = run(_eraser(store).erase(TENANT, SUBJECT))
    second = run(_eraser(store).erase(TENANT, SUBJECT))
    assert run(store.get("invoices", "inv_1"))["buyer_name"] == "Alice Al-Otaibi"
    assert first["already_absent"] is False and second["already_absent"] is False


# ── business identifiers + isolation ────────────────────────────────────────────
def test_a_business_vat_number_is_retained():
    store = MemoryDataStore()
    _invoice(store, buyer_vat="310000000000003", buyer_cr="1010101010")
    ack = run(_eraser(store).erase(TENANT, SUBJECT))
    assert run(store.get("invoices", "inv_1"))["buyer_vat"] == "310000000000003"
    assert any("buyer_vat" in r["what"] for r in ack["retained"])


def test_a_b2c_invoice_declares_no_business_identifiers():
    store = MemoryDataStore()
    _invoice(store)
    ack = run(_eraser(store).erase(TENANT, SUBJECT))
    assert not any("buyer_vat" in r["what"] for r in ack["retained"])


def test_only_this_tenants_invoices_are_touched():
    store = MemoryDataStore()
    _invoice(store, id="mine")
    run(store.put("invoices", "theirs",
                  {"id": "theirs", "tenant": "t2", "buyer_name": "Bob", "created": "2010-01-01T00:00:00+00:00"}))
    run(_eraser(store).erase(TENANT, SUBJECT))
    assert run(store.get("invoices", "theirs"))["buyer_name"] == "Bob"
