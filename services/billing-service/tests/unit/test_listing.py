"""Listing: filters, pagination, and the wall between tenants.

The write side of billing was well built and the read side was not. A UI could change a price and
never list prices; it could raise an invoice and never filter invoices; and a platform operator
could not look across tenants at all. These tests cover what a screen actually needs, and — more
importantly — what a listing must never do.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.stores import InvoiceStore, PlanStore, SubscriptionStore
from app.core.models import Invoice, Plan


def run(c):
    return asyncio.run(c)


def _seed_invoices(st, tenant="acme", n=5, status="open"):
    store = InvoiceStore(st)
    made = []
    for i in range(n):
        inv = Invoice(id=f"inv_{tenant}_{i}", tenant=tenant, number=f"INV-{i:04d}",
                      total_minor=1000 * (i + 1), currency="SAR", status=status,
                      created=f"2026-07-{i + 1:02d}T00:00:00Z")
        run(store.save(inv))
        made.append(inv)
    return store, made


# ── filtering ─────────────────────────────────────────────────────────────────
def test_invoices_can_be_filtered_by_status():
    """A finance page's first question is "what is unpaid?" and it could not be asked."""
    st = MemoryDataStore()
    store, _ = _seed_invoices(st, n=3, status="open")
    _seed_invoices(st, n=2, status="paid")
    rows, _ = run(store.browse("acme", where={"status": "paid"}))
    assert rows and all(r["status"] == "paid" for r in rows)


def test_an_empty_filter_value_is_ignored_rather_than_matching_empty_string():
    # A UI that sends `status=` for "no filter" must not get the rows whose status is literally "".
    st = MemoryDataStore()
    store, made = _seed_invoices(st, n=3)
    rows, _ = run(store.browse("acme", where={"status": None, "subscription_id": ""}))
    assert len(rows) == len(made)


def test_filtering_by_a_field_that_is_not_whitelisted_is_refused():
    """Passing query parameters into the datastore turns a list endpoint into an arbitrary query
    engine over other people's rows."""
    st = MemoryDataStore()
    store, _ = _seed_invoices(st)
    with pytest.raises(ValueError, match="NOT_FILTERABLE"):
        run(store.browse("acme", where={"total_minor": 1000}))
    with pytest.raises(ValueError, match="NOT_FILTERABLE"):
        run(store.browse("acme", where={"tenant": "someone-else"}))


def test_the_tenant_cannot_be_overridden_through_the_filter():
    """The tenant is an argument, never a filter key — otherwise the wall between companies is a
    query parameter."""
    st = MemoryDataStore()
    store, _ = _seed_invoices(st, tenant="acme")
    _seed_invoices(st, tenant="globex")
    with pytest.raises(ValueError):
        run(store.browse("acme", where={"tenant": "globex"}))


def test_a_listing_never_returns_another_tenants_rows():
    st = MemoryDataStore()
    store, _ = _seed_invoices(st, tenant="acme", n=3)
    _seed_invoices(st, tenant="globex", n=4)
    rows, _ = run(store.browse("acme", limit=100))
    assert rows and all(r["tenant"] == "acme" for r in rows)


# ── pagination ────────────────────────────────────────────────────────────────
def test_a_listing_is_resumable():
    """The cursor the datastore already returned was discarded, so past the limit the remaining
    rows were invisible with nothing in the response to say so."""
    st = MemoryDataStore()
    store, made = _seed_invoices(st, n=5)
    first, cursor = run(store.browse("acme", limit=2))
    assert len(first) == 2 and cursor, "no cursor: the second page is unreachable"

    second, _ = run(store.browse("acme", limit=2, after=cursor))
    assert second
    assert {r["id"] for r in first}.isdisjoint({r["id"] for r in second}), "pages overlap"


def test_paging_through_reaches_every_row_exactly_once():
    st = MemoryDataStore()
    store, made = _seed_invoices(st, n=7)
    seen, cursor = [], None
    for _ in range(10):
        rows, cursor = run(store.browse("acme", limit=3, after=cursor))
        seen.extend(r["id"] for r in rows)
        if not cursor:
            break
    assert sorted(seen) == sorted(i.id for i in made)
    assert len(seen) == len(set(seen)), "a row was returned on two pages"


def test_the_last_page_reports_no_cursor():
    st = MemoryDataStore()
    store, _ = _seed_invoices(st, n=2)
    _, cursor = run(store.browse("acme", limit=50))
    assert not cursor


# ── subscriptions ─────────────────────────────────────────────────────────────
def test_subscriptions_are_filterable_too():
    st = MemoryDataStore()
    store = SubscriptionStore(st)
    assert "status" in store.filterable and "plan_id" in store.filterable


# ── the public catalogue ──────────────────────────────────────────────────────
def _svc(st):
    from app.adapters.pdf.null import NullPdf
    from app.adapters.tax.vat import VatCalculator
    from app.core.billing_service import BillingService
    from app.core.stores import (CouponStore, IdempotencyStore, LedgerStore, PaymentStore,
                                 SettlementStore, UsageStore)
    from tests.unit.test_billing_core import CFG, MockEInv
    return BillingService(
        plans=PlanStore(st), coupons=CouponStore(st), subs=SubscriptionStore(st),
        invoices=InvoiceStore(st), payments=PaymentStore(st), usage=UsageStore(st),
        ledger=LedgerStore(st), idempo=IdempotencyStore(st), settlements=SettlementStore(st),
        gateway=None, tax=VatCalculator("0.15"), einvoicer=MockEInv(), pdf=NullPdf(), config=CFG)


def test_the_public_catalogue_shows_listed_active_plans():
    """The missing line: every plan route needed a capability, so the pricing page — the one
    surface that most needs prices — was the one that could not read them."""
    st = MemoryDataStore()
    b = _svc(st)
    run(b.create_plan(name="Pro", prices=[{"interval": "month", "amount_minor": 10000,
                                           "currency": "SAR", "type": "flat"}]))
    plans = run(b.public_plans())
    assert len(plans) == 1 and plans[0]["name"] == "Pro"
    assert plans[0]["prices"][0]["amount_minor"] == 10000


def test_a_retired_plan_is_not_in_the_public_catalogue():
    st = MemoryDataStore()
    b = _svc(st)
    p = run(b.create_plan(name="Old", prices=[{"interval": "month", "amount_minor": 1,
                                               "currency": "SAR", "type": "flat"}]))
    run(b.set_plan_availability(p.id, active=False))
    assert run(b.public_plans()) == []


def test_an_unlisted_plan_is_not_in_the_public_catalogue():
    """`listed=False` means nothing if one route ignores it."""
    st = MemoryDataStore()
    b = _svc(st)
    p = run(b.create_plan(name="Secret", prices=[{"interval": "month", "amount_minor": 1,
                                                  "currency": "SAR", "type": "flat"}]))
    run(b.set_plan_availability(p.id, listed=False))
    assert run(b.public_plans()) == []


def test_the_public_catalogue_is_a_whitelist_not_the_stored_row():
    """A denylist would leak every field added to Plan later, and whoever added it would have no
    reason to think about this endpoint."""
    st = MemoryDataStore()
    b = _svc(st)
    # A field that does not belong to the public offer, written straight onto the stored row.
    plan = Plan(id="plan_x", name="Pro", prices=[{"interval": "month", "amount_minor": 100,
                                                  "currency": "SAR", "type": "flat"}])
    row = plan.to_dict()
    row["internal_margin_note"] = "cost basis 40%"
    run(PlanStore(st).put(plan.id, row))

    got = run(b.public_plans())
    assert got, "the plan disappeared"
    assert "internal_margin_note" not in got[0], "an internal field leaked to an anonymous caller"
    assert set(got[0]) <= set(b.PUBLIC_PLAN_FIELDS)


def test_public_prices_are_projected_too():
    st = MemoryDataStore()
    b = _svc(st)
    plan = Plan(id="plan_y", name="Pro",
                prices=[{"interval": "month", "amount_minor": 100, "currency": "SAR",
                         "type": "flat", "internal_cost_minor": 40}])
    run(PlanStore(st).put(plan.id, plan.to_dict()))
    price = run(b.public_plans())[0]["prices"][0]
    assert "internal_cost_minor" not in price


# ── the cross-tenant platform view (secure by construction) ───────────────────
def test_the_platform_view_spans_all_tenants():
    """The tenant-scoped listing answers 'this customer's invoices'; this answers 'every unpaid
    invoice on the platform', which per-tenant queries cannot without a tenant list."""
    st = MemoryDataStore()
    _seed_invoices(st, tenant="acme", n=2, status="open")
    _seed_invoices(st, tenant="globex", n=3, status="open")
    store = InvoiceStore(st)
    rows, _ = run(store.browse_platform(where={"status": "open"}, limit=100))
    tenants = {r["tenant"] for r in rows}
    assert tenants == {"acme", "globex"}


def test_the_platform_view_requires_a_filter():
    """No unfiltered ledger dump: an empty filter is a data-exfiltration primitive, not a feature."""
    st = MemoryDataStore()
    _seed_invoices(st, tenant="acme")
    with pytest.raises(ValueError, match="NEEDS_A_FILTER"):
        run(InvoiceStore(st).browse_platform(where={"status": None}, limit=100))


def test_the_platform_view_still_whitelists_filter_keys():
    st = MemoryDataStore()
    _seed_invoices(st, tenant="acme")
    with pytest.raises(ValueError, match="NOT_FILTERABLE"):
        run(InvoiceStore(st).browse_platform(where={"total_minor": 1000}, limit=100))


def test_the_platform_view_is_a_money_projection_not_pii():
    """A cross-tenant view names the tenant, amount and status — never the buyer. A field added to
    the invoice later must not leak into every operator's platform scan by default."""
    from app.adapters.pdf.null import NullPdf
    from app.adapters.tax.vat import VatCalculator
    from app.core.billing_service import BillingService
    from app.core.stores import (CouponStore, IdempotencyStore, LedgerStore, PaymentStore,
                                 SettlementStore, SubscriptionStore, UsageStore)
    from tests.unit.test_billing_core import CFG, MockEInv
    st = MemoryDataStore()
    run(InvoiceStore(st).save(Invoice(id="inv_1", tenant="acme", number="INV-0001",
                                      total_minor=1000, currency="SAR", status="open",
                                      created="2026-07-01")))
    # A buyer name written onto the row must not appear in the platform view.
    row = run(st.get("invoices", "inv_1")); row["buyer_name"] = "Jane Private"
    run(st.put("invoices", "inv_1", row))

    b = BillingService(plans=PlanStore(st), coupons=CouponStore(st), subs=SubscriptionStore(st),
                       invoices=InvoiceStore(st), payments=PaymentStore(st), usage=UsageStore(st),
                       ledger=LedgerStore(st), idempo=IdempotencyStore(st),
                       settlements=SettlementStore(st), gateway=None, tax=VatCalculator("0.15"),
                       einvoicer=MockEInv(), pdf=NullPdf(), config=CFG)
    out = run(b.list_invoices_platform(status="open"))["invoices"][0]
    assert "buyer_name" not in out
    assert set(out) <= set(b.PLATFORM_INVOICE_FIELDS)
    assert out["tenant"] == "acme" and out["total_minor"] == 1000
