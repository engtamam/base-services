"""One-off wallet top-up — a shop owner buys prepaid credit that lands in a wallet fund.

Money invariants under test:
  - default is a RECEIPT: no VAT at top-up, the fund is credited the exact amount (VAT is applied
    when the credit is spent — taxing it here too would double-tax);
  - the credit goes to the REQUESTED fund (not the plan grant fund), reusing the durable grant outbox;
  - charge-first / persist-on-success: a decline burns no invoice and records no grant;
  - tax_invoice mode adds VAT and credits the gross;
  - amount and cap are validated; a gateway outage is retryable, not a decline.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.tax.vat import VatCalculator
from app.core.billing_service import (BillingService, UpgradeChargeFailed, UpgradeChargeUnavailable)
from app.core.grants import GrantOutbox, source_for
from app.core.models import iso, now_ts
from app.core.stores import (CouponStore, IdempotencyStore, InvoiceStore, LedgerStore, PaymentStore,
                             PlanStore, SettlementStore, SubscriptionStore, UsageStore)
from app.ports.payment_gateway import Charge, GatewayUnavailable

from tests.unit.test_billing_core import CFG, MockEInv, NullPdf


def run(c):
    return asyncio.run(c)


class _GW:
    driver = "fake"

    def __init__(self, ok=True, unavailable=False):
        self.ok, self.unavailable, self.charged = ok, unavailable, []

    def charge(self, **k):
        self.charged.append(k)
        if self.unavailable:
            raise GatewayUnavailable("down")
        return Charge(ok=self.ok, status="succeeded" if self.ok else "failed", provider="fake",
                      provider_id="prov_1", amount_minor=k["amount_minor"], currency=k["currency"],
                      error="" if self.ok else "declined")


def _outbox(st):
    return GrantOutbox(st, None, clock=now_ts, iso=iso,
                       cfg={"grant_fund": "credits", "grant_retry_base_seconds": 30,
                            "grant_retry_max_seconds": 3600})


def _svc(st, gw, *, tax_mode="receipt", max_minor=0, grants=None):
    cfg = dict(CFG, grant_fund="credits", wallet_topup_fund="topup",
               wallet_topup_tax_mode=tax_mode, wallet_topup_max_minor=max_minor)
    return BillingService(plans=PlanStore(st), coupons=CouponStore(st), subs=SubscriptionStore(st),
                          invoices=InvoiceStore(st), payments=PaymentStore(st), usage=UsageStore(st),
                          ledger=LedgerStore(st), idempo=IdempotencyStore(st),
                          settlements=SettlementStore(st), gateway=gw, tax=VatCalculator("0.15"),
                          einvoicer=MockEInv(), pdf=NullPdf(), config=cfg,
                          grants=grants if grants is not None else _outbox(st))


def _grant(st, inv_id):
    return run(st.get("credit_grants", source_for(inv_id)))


# ── receipt (default): no VAT, exact fund credit ─────────────────────────────

def test_a_topup_credits_the_requested_fund_with_no_vat():
    st = MemoryDataStore()
    gw = _GW()
    b = _svc(st, gw)
    out = run(b.wallet_topup(tenant="acme", amount_minor=10_000, payment_token="tok", fund="topup"))
    assert out["status"] == "succeeded" and out["amount_minor"] == 10_000
    assert out["fund"] == "topup" and out["doc_type"] == "receipt"
    assert gw.charged[0]["amount_minor"] == 10_000          # charged the exact amount, no VAT added
    g = _grant(st, out["invoice_id"])
    assert g["fund"] == "topup" and g["amount_minor"] == 10_000     # credited the requested fund
    inv = run(InvoiceStore(st).fetch(out["invoice_id"]))
    assert inv.tax_minor == 0 and inv.total_minor == 10_000 and inv.subscription_id == ""


def test_an_empty_fund_falls_back_to_the_configured_default():
    st = MemoryDataStore()
    b = _svc(st, _GW())
    out = run(b.wallet_topup(tenant="acme", amount_minor=5_000, payment_token="tok"))
    assert out["fund"] == "topup"                            # WALLET_TOPUP_FUND default


# ── tax_invoice mode: VAT added, gross credited ──────────────────────────────

def test_tax_invoice_mode_adds_vat_and_credits_the_gross():
    st = MemoryDataStore()
    gw = _GW()
    b = _svc(st, gw, tax_mode="tax_invoice")
    out = run(b.wallet_topup(tenant="acme", amount_minor=10_000, payment_token="tok", fund="topup"))
    assert gw.charged[0]["amount_minor"] == 11_500          # 10000 + 15% VAT
    inv = run(InvoiceStore(st).fetch(out["invoice_id"]))
    assert inv.tax_minor == 1_500 and inv.total_minor == 11_500
    assert _grant(st, out["invoice_id"])["amount_minor"] == 11_500   # the credit they bought (gross)


# ── charge-first / persist-on-success ────────────────────────────────────────

def test_a_decline_records_no_invoice_and_no_grant():
    st = MemoryDataStore()
    b = _svc(st, _GW(ok=False))
    with pytest.raises(UpgradeChargeFailed):
        run(b.wallet_topup(tenant="acme", amount_minor=10_000, payment_token="tok", fund="topup"))
    # nothing persisted: no invoices, no payments, no grants
    assert run(st.scan_all("invoices", 50)) == []
    assert run(st.scan_all("payments", 50)) == []
    assert run(st.scan_all("credit_grants", 50)) == []


def test_a_gateway_outage_is_retryable_not_a_decline():
    st = MemoryDataStore()
    b = _svc(st, _GW(unavailable=True))
    with pytest.raises(UpgradeChargeUnavailable):
        run(b.wallet_topup(tenant="acme", amount_minor=10_000, payment_token="tok", fund="topup"))


# ── validation ───────────────────────────────────────────────────────────────

def test_a_non_positive_amount_is_refused():
    st = MemoryDataStore()
    b = _svc(st, _GW())
    with pytest.raises(ValueError):
        run(b.wallet_topup(tenant="acme", amount_minor=0, payment_token="tok", fund="topup"))


def test_an_amount_over_the_cap_is_refused():
    st = MemoryDataStore()
    gw = _GW()
    b = _svc(st, gw, max_minor=50_000)
    with pytest.raises(ValueError):
        run(b.wallet_topup(tenant="acme", amount_minor=60_000, payment_token="tok", fund="topup"))
    assert gw.charged == []                                 # refused BEFORE charging


def test_a_missing_payment_token_is_refused():
    st = MemoryDataStore()
    b = _svc(st, _GW())
    with pytest.raises(UpgradeChargeFailed):
        run(b.wallet_topup(tenant="acme", amount_minor=10_000, payment_token="", fund="topup"))


# ── hosted-checkout top-up (TuwaiqPay-style: no token, poll to complete) ──────

from app.core.models import Payment      # noqa: E402
from app.ports.payment_gateway import CheckoutSession   # noqa: E402


class _HostedGW:
    """A hosted-link gateway: no token charge (raises), a checkout session, and a pollable status."""
    driver = "tuwaiq"

    def __init__(self, session_ok=True, status="succeeded"):
        self.session_ok, self._status = session_ok, status

    def charge(self, **k):
        raise GatewayUnavailable("hosted-link only; no token charge")

    def prepare_checkout(self, *, amount_minor, currency, reference, return_url="", metadata=None):
        if not self.session_ok:
            return CheckoutSession(ok=False, provider="tuwaiq", error="NO_SESSION")
        return CheckoutSession(ok=True, provider="tuwaiq", id="bill_" + reference[:6],
                               redirect_url="https://pay/xyz")

    def fetch_status(self, *, provider_id, currency=""):
        return Charge(ok=True, status=self._status, provider="tuwaiq", provider_id=provider_id)


def test_checkout_topup_persists_a_pending_invoice_and_payment_with_grant_meta():
    st = MemoryDataStore()
    b = _svc(st, _HostedGW())
    out = run(b.wallet_topup_checkout(tenant="acme", amount_minor=30_000, fund="topup"))
    assert out["status"] == "pending" and out["checkout"]["redirect_url"] == "https://pay/xyz"
    inv = run(InvoiceStore(st).fetch(out["invoice_id"]))
    assert inv.status == "open" and inv.total_minor == 30_000 and inv.tax_minor == 0
    assert inv.meta["grant_fund"] == "topup" and inv.meta["grant_minor"] == 30_000
    pay = run(PaymentStore(st).fetch(out["payment_id"]))
    assert pay.status == "pending" and pay.provider_id.startswith("bill_")
    assert _grant(st, out["invoice_id"]) is None            # NOT credited yet — still pending


def test_a_checkout_topup_credits_the_fund_only_when_the_poll_confirms_payment():
    st = MemoryDataStore()
    b = _svc(st, _HostedGW(status="succeeded"))
    out = run(b.wallet_topup_checkout(tenant="acme", amount_minor=30_000, fund="topup"))
    res = run(b.reconcile_payment(out["payment_id"]))       # the poll/verify step
    assert res["reconciled"] is True
    g = _grant(st, out["invoice_id"])
    assert g["fund"] == "topup" and g["amount_minor"] == 30_000   # credited exactly once, on confirm
    assert run(InvoiceStore(st).fetch(out["invoice_id"])).status == "paid"


def test_a_checkout_topup_reconcile_is_idempotent():
    st = MemoryDataStore()
    b = _svc(st, _HostedGW(status="succeeded"))
    out = run(b.wallet_topup_checkout(tenant="acme", amount_minor=30_000, fund="topup"))
    run(b.reconcile_payment(out["payment_id"]))
    second = run(b.reconcile_payment(out["payment_id"]))    # a re-poll must not double-credit
    assert second["reconciled"] is False
    assert _grant(st, out["invoice_id"])["amount_minor"] == 30_000


def test_a_gateway_that_cannot_open_a_session_leaves_no_orphan_invoice():
    st = MemoryDataStore()
    b = _svc(st, _HostedGW(session_ok=False))
    with pytest.raises(UpgradeChargeFailed):
        run(b.wallet_topup_checkout(tenant="acme", amount_minor=30_000, fund="topup"))
    assert run(st.scan_all("invoices", 50)) == [] and run(st.scan_all("payments", 50)) == []
