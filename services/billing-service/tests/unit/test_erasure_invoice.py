"""The case the whole phase was written for: the invoice stays — and DURING retention it stays whole.

Tax law says keep the invoice 6–10 years; privacy law says erase the person. Both are mandatory, so
deleting the invoice is illegal — and, during the retention window, editing it is wrong too: buyer_name
is a MANDATORY field on a standard ZATCA tax invoice, and the retained copy must match the document
exactly as issued (the same principle zatca-service applies to its signed XML). The name becomes
eligible for pseudonymisation only once `created + retention_years` has passed.
"""
from __future__ import annotations

import asyncio
import datetime as dt

from app.adapters.datastore.memory import MemoryDataStore
from app.core.erasure import BillingErasure
from app.core.pii import subject_ref

SALT = "product-pii-salt"
TENANT, SUBJECT = "t1", "alice@x.com"
REF = subject_ref(SALT, TENANT, SUBJECT)


def run(c):
    return asyncio.run(c)


def _ts(iso: str) -> float:
    return dt.datetime.fromisoformat(iso).timestamp()


def _eraser(store, *, years=10, at="2026-01-01T00:00:00+00:00"):
    # `at` is the wall clock the erasure runs at, so a test can place an invoice inside or past its
    # retention window deterministically.
    return BillingErasure(store=store, pii_salt=SALT, retention_years=years, clock=lambda: _ts(at))


def _invoice(store, **over):
    inv = {"id": over.get("id", "inv_1"), "tenant": TENANT, "buyer_name": "Alice Al-Otaibi",
           "buyer_vat": "", "buyer_cr": "", "total_minor": 115_00, "vat_minor": 15_00,
           "created": over.get("created", "2025-06-01T00:00:00+00:00")}
    inv.update(over)
    run(store.put("invoices", inv["id"], inv))
    return inv


# ── within the retention window: touch NOTHING ──────────────────────────────────
def test_within_retention_the_invoice_and_the_name_are_left_unaltered():
    """The retained copy must match the invoice as ISSUED — buyer_name is a mandatory ZATCA field.
    Editing it during the window is the exact defect: our copy would no longer match what was filed."""
    store = MemoryDataStore()
    _invoice(store, created="2025-06-01T00:00:00+00:00")           # ~7 months old, 10y retention

    run(_eraser(store).erase(TENANT, SUBJECT))

    kept = run(store.get("invoices", "inv_1"))
    assert kept["buyer_name"] == "Alice Al-Otaibi", "a legally-retained mandatory field was edited early"
    assert kept["total_minor"] == 115_00 and kept["vat_minor"] == 15_00


def test_within_retention_the_ack_says_the_whole_record_is_retained_unaltered():
    store = MemoryDataStore()
    _invoice(store)
    ack = run(_eraser(store).erase(TENANT, SUBJECT))

    assert ack["erased"] == []
    assert ack["already_absent"] is False              # we DID find the person's data; we retained it
    entry = next(r for r in ack["retained"] if r["what"].startswith("invoices:"))
    assert entry["basis"] == "tax_retention"
    why = entry["why"].lower()
    assert "unaltered" in why and "name" in why and "mandatory" in why


def test_within_retention_erasure_is_a_no_op_and_stays_already_present_on_retry():
    """A DSAR retries. During retention nothing is written, and the invoice keeps matching, so the
    honest answer both times is 'found and retained', never a false already_absent."""
    store = MemoryDataStore()
    _invoice(store)
    first = run(_eraser(store).erase(TENANT, SUBJECT))
    second = run(_eraser(store).erase(TENANT, SUBJECT))

    assert run(store.get("invoices", "inv_1"))["buyer_name"] == "Alice Al-Otaibi"
    assert first["already_absent"] is False and second["already_absent"] is False


# ── after the retention window: the name becomes eligible for pseudonymisation ──────────────
def test_after_retention_the_name_is_pseudonymised_and_the_record_kept():
    store = MemoryDataStore()
    _invoice(store, created="2010-01-01T00:00:00+00:00")          # 16y old at the 2026 clock, 10y retention

    ack = run(_eraser(store).erase(TENANT, SUBJECT))

    kept = run(store.get("invoices", "inv_1"))
    assert kept["buyer_name"] == REF and "Alice" not in str(kept)
    assert kept["total_minor"] == 115_00                          # the financial record still survives
    entry = next(r for r in ack["retained"] if r["what"].startswith("invoices:"))
    assert entry["basis"] == "tax_retention" and "expired" in entry["why"].lower()


def test_after_retention_pseudonymisation_is_idempotent():
    store = MemoryDataStore()
    _invoice(store, created="2010-01-01T00:00:00+00:00")
    run(_eraser(store).erase(TENANT, SUBJECT))
    second = run(_eraser(store).erase(TENANT, SUBJECT))           # name is now REF -> no longer matches

    assert run(store.get("invoices", "inv_1"))["buyer_name"] == REF   # not double-wrapped
    assert second["already_absent"] is True


# ── business identifiers and isolation (unchanged by the retention rule) ────────────────────
def test_a_business_vat_number_is_retained_because_the_invoice_needs_it():
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
    _invoice(store, id="mine", created="2010-01-01T00:00:00+00:00")     # past retention -> would edit
    run(store.put("invoices", "theirs",
                  {"id": "theirs", "tenant": "t2", "buyer_name": "Bob",
                   "created": "2010-01-01T00:00:00+00:00"}))
    run(_eraser(store).erase(TENANT, SUBJECT))
    assert run(store.get("invoices", "theirs"))["buyer_name"] == "Bob"


def test_an_undatable_invoice_is_kept_intact_fail_safe():
    """If we cannot establish the issue date, we must NOT risk editing a legally-retained field."""
    store = MemoryDataStore()
    _invoice(store, created="")
    run(_eraser(store).erase(TENANT, SUBJECT))
    assert run(store.get("invoices", "inv_1"))["buyer_name"] == "Alice Al-Otaibi"


def test_the_retention_period_is_config_not_a_hardcoded_law():
    """ZATCA/KSA is 6; several EU states are 10. The jurisdiction decides, not us."""
    store = MemoryDataStore()
    _invoice(store)
    ack = run(_eraser(store, years=6).erase(TENANT, SUBJECT))
    assert "6 years" in next(r for r in ack["retained"] if r["what"].startswith("invoices:"))["why"]
