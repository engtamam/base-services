"""The case the whole phase was written for: the invoice stays, the person goes.

Tax law says keep it 6–10 years. Privacy law says erase the person. Both are mandatory, so
deleting the invoice is illegal and leaving the buyer's name on it is also illegal.

Keep the record; pseudonymise the human on it.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.erasure import BillingErasure
from app.core.pii import subject_ref

SALT = "product-pii-salt"
TENANT, SUBJECT = "t1", "alice@x.com"


def run(c):
    return asyncio.run(c)


def _fixture():
    store = MemoryDataStore()
    return store, BillingErasure(store=store, pii_salt=SALT, retention_years=10)


def _invoice(store, **over):
    inv = {"id": over.get("id", "inv_1"), "tenant": TENANT, "buyer_name": "Alice Al-Otaibi",
           "buyer_vat": "", "buyer_cr": "", "total_minor": 115_00, "vat_minor": 15_00,
           "created": over.get("created", "2026-01-01")}
    inv.update(over)
    run(store.put("invoices", inv["id"], inv))
    return inv


def test_the_invoice_survives_and_the_totals_are_untouched():
    """A tax inspector checks the arithmetic. They do not need to know who the buyer was."""
    store, eraser = _fixture()
    _invoice(store)

    run(eraser.erase(TENANT, SUBJECT))

    kept = run(store.get("invoices", "inv_1"))
    assert kept is not None, "the invoice was DELETED — that is a tax-law violation"
    assert kept["total_minor"] == 115_00 and kept["vat_minor"] == 15_00


def test_the_buyers_name_is_replaced_by_the_pseudonym():
    store, eraser = _fixture()
    _invoice(store)

    run(eraser.erase(TENANT, SUBJECT))

    kept = run(store.get("invoices", "inv_1"))
    assert kept["buyer_name"] == subject_ref(SALT, TENANT, SUBJECT)
    assert "Alice" not in str(kept), "the person's name is still on the retained invoice"


def test_billing_erases_nothing_and_says_so_with_a_law_attached():
    """"We kept it" must be an answer with a reason — a silence is how a person gets lied to."""
    store, eraser = _fixture()
    _invoice(store)

    ack = run(eraser.erase(TENANT, SUBJECT))

    assert ack["erased"] == []
    assert ack["retained"], "billing kept the invoice and did not declare it"
    assert ack["retained"][0]["basis"] == "tax_retention"
    assert "10 years" in ack["retained"][0]["why"], "the retention period is not quoted back"


def test_a_business_vat_number_is_retained_because_the_invoice_needs_it():
    """A VAT number identifies a BUSINESS. Stripping it invalidates the invoice we must keep."""
    store, eraser = _fixture()
    _invoice(store, buyer_vat="310000000000003", buyer_cr="1010101010")

    ack = run(eraser.erase(TENANT, SUBJECT))

    kept = run(store.get("invoices", "inv_1"))
    assert kept["buyer_vat"] == "310000000000003", "the VAT number was stripped from a tax invoice"
    assert any("buyer_vat" in r["what"] for r in ack["retained"])


def test_a_b2c_invoice_declares_no_business_identifiers():
    store, eraser = _fixture()
    _invoice(store)                       # no vat, no cr
    ack = run(eraser.erase(TENANT, SUBJECT))
    assert not any("buyer_vat" in r["what"] for r in ack["retained"])


def test_erasure_is_idempotent_and_does_not_re_pseudonymise():
    """A DSAR retries. Folding a pseudonym again would produce sub_(sub_(x)) and break the link."""
    store, eraser = _fixture()
    _invoice(store)

    first = run(eraser.erase(TENANT, SUBJECT))
    second = run(eraser.erase(TENANT, SUBJECT))

    kept = run(store.get("invoices", "inv_1"))
    assert kept["buyer_name"] == subject_ref(SALT, TENANT, SUBJECT)   # not double-wrapped
    assert first["already_absent"] is False
    assert second["already_absent"] is True


def test_only_this_tenants_invoices_are_touched():
    store, eraser = _fixture()
    _invoice(store, id="mine")
    run(store.put("invoices", "theirs",
                  {"id": "theirs", "tenant": "t2", "buyer_name": "Bob", "created": "2026-01-01"}))

    run(eraser.erase(TENANT, SUBJECT))

    other = run(store.get("invoices", "theirs"))
    assert other["buyer_name"] == "Bob", "another tenant's invoice was rewritten"


def test_the_retention_period_is_config_not_a_hardcoded_law():
    """ZATCA/KSA is 6; several EU states are 10. The jurisdiction decides, not us."""
    store = MemoryDataStore()
    _invoice(store)
    ack = run(BillingErasure(store=store, pii_salt=SALT, retention_years=6)
              .erase(TENANT, SUBJECT))
    assert "6 years" in ack["retained"][0]["why"]
