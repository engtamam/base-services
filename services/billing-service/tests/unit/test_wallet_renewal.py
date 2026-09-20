"""Renewal by wallet debit — a subscription pinned to the "wallet" gateway renews by debiting the
customer's prepaid balance instead of charging a card (for gateways with no recurring, e.g.
TuwaiqPay).

Every test names a money footgun this must not have:
  - re-pricing the debit (double-taxing) instead of debiting the invoice total as-is;
  - retrying an empty prepaid balance for ever (a card decline is retryable; a balance is not);
  - suspending a paying customer for a transient wallet OUTAGE;
  - silently taking free service when the feature is on but the fund is unconfigured;
  - draining the wallet when the feature is OFF or the sub did not opt in.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.tax.vat import VatCalculator
from app.core import events as _events
from app.core.billing_service import BillingService
from app.core.models import Invoice, Subscription
from app.core.stores import (CouponStore, IdempotencyStore, InvoiceStore, LedgerStore, PaymentStore,
                             PlanStore, SettlementStore, SubscriptionStore, UsageStore)
from app.ports.payment_gateway import Charge
from app.ports.wallet import WalletRefused, WalletUnavailable

from tests.unit.test_billing_core import CFG, MockEInv, NullPdf


def run(c):
    return asyncio.run(c)


class _GW:
    """The real gateway — must NOT be touched when a wallet renewal handles the invoice."""
    driver = "moyasar"

    def __init__(self):
        self.charged = []

    def charge(self, **k):
        self.charged.append(k)
        return Charge(ok=True, status="succeeded", provider="moyasar", provider_id="ch_1",
                      amount_minor=k["amount_minor"], currency=k["currency"])


class _Wallet:
    """A fake wallet. `mode` picks the debit outcome: ok | insufficient | unavailable."""

    def __init__(self, mode="ok"):
        self.mode = mode
        self.debits = []

    async def debit(self, *, tenant, fund, amount_minor, source, reason="", allow_negative=False):
        self.debits.append({"tenant": tenant, "fund": fund, "amount_minor": amount_minor,
                            "source": source, "allow_negative": allow_negative})
        if self.mode == "insufficient":
            raise WalletRefused("wallet refused the debit (status 409)")
        if self.mode == "unavailable":
            raise WalletUnavailable("wallet unavailable (status 503)")
        return True

    async def credit(self, **k):
        return True

    async def health(self):
        return True


class _Emitter:
    def __init__(self):
        self.events = []

    async def emit(self, event_type, payload):
        self.events.append((event_type, payload))


def _svc(st, *, wallet=None, enabled=True, fund="renewal", gw=None, emitter=None):
    cfg = dict(CFG, wallet_renewal_enabled=enabled, wallet_renewal_fund=fund)
    return BillingService(plans=PlanStore(st), coupons=CouponStore(st), subs=SubscriptionStore(st),
                          invoices=InvoiceStore(st), payments=PaymentStore(st), usage=UsageStore(st),
                          ledger=LedgerStore(st), idempo=IdempotencyStore(st),
                          settlements=SettlementStore(st), gateway=gw or _GW(),
                          tax=VatCalculator("0.15"), einvoicer=MockEInv(), pdf=NullPdf(), config=cfg,
                          emitter=emitter, wallet=wallet)


def _inv(tenant="t1"):
    # subtotal 10000 + 15% VAT = 11500 gross. The debit must be the GROSS, not a re-priced amount.
    return Invoice(id="inv_x", tenant=tenant, number="INV-1", subscription_id="sub_x",
                   subtotal_minor=10000, tax_minor=1500, total_minor=11500, currency="SAR",
                   status="open", created="2026-01-01")


def _sub(gateway="wallet", status="active"):
    return Subscription(id="sub_x", tenant="t1", plan_id="p", status=status,
                        payment_token="", payment_gateway=gateway)


# ── the happy path ──────────────────────────────────────────────────────────

def test_a_wallet_pinned_sub_debits_the_wallet_not_the_gateway():
    st = MemoryDataStore()
    w, gw = _Wallet("ok"), _GW()
    b = _svc(st, wallet=w, gw=gw)
    run(SubscriptionStore(st).save(_sub()))
    run(b._charge_invoice(_inv(), "", _sub()))
    assert len(w.debits) == 1 and not gw.charged            # wallet used, gateway untouched
    inv = run(InvoiceStore(st).fetch("inv_x"))
    assert inv.status == "paid"


def test_the_debit_is_the_invoice_gross_exactly_never_re_priced():
    st = MemoryDataStore()
    w = _Wallet("ok")
    b = _svc(st, wallet=w)
    run(b._charge_invoice(_inv(), "", _sub()))
    d = w.debits[0]
    assert d["amount_minor"] == 11500          # the GROSS (subtotal+VAT), not re-taxed
    assert d["fund"] == "renewal" and d["allow_negative"] is False
    assert d["source"] == "inv_x"              # idempotent on the invoice id


# ── insufficient balance: immediate suspend, no retry ───────────────────────

def test_insufficient_balance_suspends_immediately_and_does_not_retry():
    st = MemoryDataStore()
    w, em = _Wallet("insufficient"), _Emitter()
    b = _svc(st, wallet=w, emitter=em)
    run(SubscriptionStore(st).save(_sub()))
    inv = _inv()
    run(b._charge_invoice(inv, "", _sub()))
    sub = run(SubscriptionStore(st).fetch("sub_x"))
    assert sub.status == "past_due"                          # suspended
    # a distinct top-up event fired (not payment.failed, which implies a retry is coming). The lapse
    # also emits the uniform subscription.suspended alongside it, so assert PRESENCE, not position.
    bytype = {t: p for t, p in em.events}
    assert _events.SUBSCRIPTION_RENEWAL_WALLET_INSUFFICIENT in bytype
    assert bytype[_events.SUBSCRIPTION_RENEWAL_WALLET_INSUFFICIENT]["amount_minor"] == 11500
    assert bytype[_events.SUBSCRIPTION_SUSPENDED]["reason"] == "wallet_insufficient"
    assert inv.status != "paid"                              # never marked paid


def test_insufficient_balance_never_marks_the_invoice_paid_no_free_service():
    st = MemoryDataStore()
    b = _svc(st, wallet=_Wallet("insufficient"))
    run(SubscriptionStore(st).save(_sub()))
    inv = _inv()
    run(b._charge_invoice(inv, "", _sub()))
    assert inv.status != "paid"


# ── transient wallet outage: retry, do NOT suspend ──────────────────────────

def test_a_wallet_outage_dunns_and_does_not_suspend_a_paying_customer():
    st = MemoryDataStore()
    w, em = _Wallet("unavailable"), _Emitter()
    b = _svc(st, wallet=w, emitter=em)
    run(SubscriptionStore(st).save(_sub()))
    run(b._charge_invoice(_inv(), "", _sub()))
    # dunning was entered (a retry is scheduled), and NO immediate wallet-insufficient event fired
    assert all(e[0] != _events.SUBSCRIPTION_RENEWAL_WALLET_INSUFFICIENT for e in em.events)
    sub = run(SubscriptionStore(st).fetch("sub_x"))
    assert sub.status == "active"                            # not suspended for a transient outage


# ── misconfiguration: fail loud, never free ─────────────────────────────────

def test_opted_in_but_no_fund_configured_fails_loud_and_charges_nothing():
    st = MemoryDataStore()
    w, gw = _Wallet("ok"), _GW()
    b = _svc(st, wallet=w, fund="", gw=gw)             # feature on, sub opted in, but NO fund named
    with pytest.raises(WalletUnavailable):
        run(b._charge_invoice(_inv(), "", _sub()))
    assert not w.debits and not gw.charged             # neither wallet nor gateway took money
    assert run(InvoiceStore(st).fetch("inv_x")) is None or \
        run(InvoiceStore(st).fetch("inv_x")).status != "paid"


# ── the feature is opt-in and off by default ────────────────────────────────

def test_feature_off_falls_through_to_the_gateway():
    st = MemoryDataStore()
    w, gw = _Wallet("ok"), _GW()
    b = _svc(st, wallet=w, enabled=False, gw=gw)
    run(b._charge_invoice(_inv(), "tok", _sub()))
    assert not w.debits and gw.charged                 # wallet untouched, gateway charged


def test_a_non_wallet_pinned_sub_uses_the_gateway_even_when_the_feature_is_on():
    st = MemoryDataStore()
    w, gw = _Wallet("ok"), _GW()
    b = _svc(st, wallet=w, gw=gw)
    run(b._charge_invoice(_inv(), "tok", _sub(gateway="moyasar")))
    assert not w.debits and gw.charged


def test_no_wallet_wired_falls_through_to_the_gateway():
    st = MemoryDataStore()
    gw = _GW()
    b = _svc(st, wallet=None, gw=gw)
    run(b._charge_invoice(_inv(), "tok", _sub()))
    assert gw.charged                                  # a wallet-pinned sub with no wallet wired


# ── dunning sweep retries through the WALLET, not a non-existent gateway ─────

def test_dunning_sweep_retries_a_wallet_sub_through_the_wallet_and_reactivates():
    from app.core.models import Payment
    st = MemoryDataStore()
    w, gw = _Wallet("ok"), _GW()
    b = _svc(st, wallet=w, gw=gw)
    # a past_due wallet sub with an open invoice and a failed payment due for retry
    run(SubscriptionStore(st).save(_sub(status="past_due")))
    inv = _inv(); inv.status = "open"
    run(InvoiceStore(st).save(inv))
    run(PaymentStore(st).save(Payment(id="pay_1", tenant="t1", invoice_id="inv_x", status="failed",
                                      next_retry_at=1.0, error="WALLET_UNAVAILABLE")))
    run(b.dunning_sweep())
    assert len(w.debits) == 1 and not gw.charged            # retried via wallet, never the gateway
    assert run(InvoiceStore(st).fetch("inv_x")).status == "paid"
    assert run(SubscriptionStore(st).fetch("sub_x")).status == "active"   # reactivated
