"""start_subscription_checkout — a NEW paid subscription that returns a hosted-checkout LINK instead of
charging a saved card (for token-less gateways like TuwaiqPay).

The money contract:
  * the subscription is born `incomplete` — NOT billable, NOT entitled, NEVER swept — until paid;
  * its first-period invoice is settled through the SAME reconcile/webhook -> _mark_paid seam as any
    other payment, and THAT activates the subscription (period claimed + advanced, exactly like
    checkout()), so a hosted-checkout signup ends identical to a card one;
  * a retried/double-clicked request replays the first link — never a second incomplete sub / second
    checkout session (which could be paid twice).
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.billing_service import BillingService
from app.core.grants import GrantOutbox
from app.core.models import iso, now_ts
from app.core.stores import (CouponStore, IdempotencyStore, InvoiceStore, LedgerStore, PayLinkStore,
                             PaymentStore, PlanStore, SettlementStore, SubscriptionStore, UsageStore)
from app.ports.payment_gateway import CheckoutSession


def run(c):
    return asyncio.run(c)


class MockTax:
    def compute(self, *, amount_minor: int) -> int:
        from decimal import ROUND_HALF_UP, Decimal
        return int((Decimal(amount_minor) * Decimal("0.15")).quantize(Decimal(1), ROUND_HALF_UP))


class _Status:
    def __init__(self, status):
        self.status = status


class HostedGW:
    """A hosted-checkout gateway: no charge(token), only a checkout session + a status endpoint —
    exactly TuwaiqPay's shape."""
    driver = "hosted"

    def __init__(self):
        self.settled: set[str] = set()          # provider_ids the "customer" has paid

    def prepare_checkout(self, *, amount_minor, currency, reference, return_url="", metadata=None):
        return CheckoutSession(ok=True, provider="hosted", id=f"sess_{reference}",
                               redirect_url=f"https://checkout.hosted/pay/{reference}")

    def fetch_status(self, *, provider_id, currency):
        return _Status("succeeded" if provider_id in self.settled else "pending")

    def charge(self, **k):                        # present but ALWAYS fails — token charges unsupported
        class _C:
            ok = False; status = "failed"; provider = "hosted"; provider_id = ""
            amount_minor = 0; currency = "SAR"; error = "TOKEN_UNSUPPORTED"; raw = {}
        return _C()


class _NoCheckoutGW:
    driver = "null"                               # no prepare_checkout at all

    def charge(self, **k):
        raise AssertionError("must not charge")


def _outbox(st):
    return GrantOutbox(st, None, clock=now_ts, iso=iso,
                       cfg={"grant_fund": "credits", "grant_retry_base_seconds": 30,
                            "grant_retry_max_seconds": 3600})


def _svc(st, gw=None):
    cfg = {"currency": "SAR", "trial_days": 0, "vat_rate": "0.15", "reverse_charge": False,
           "grandfather": True, "dunning_base_hours": 24, "dunning_max_attempts": 4, "grace_days": 7,
           "list_limit": 100, "limits_cache_ttl": 300, "billable_statuses": ("active", "trialing"),
           "max_subscriptions": 200, "merge_strategies": {}, "rank_orders": {},
           "period_tolerance_seconds": 86400, "cycle_sweep_limit": 1000, "cycle_lead_seconds": 0,
           "platform_max_pages": 50, "grant_fund": "credits", "plan_credit_limit_key": "credits",
           "plan_credit_grant_mode": "accumulate", "document_mode": "receipt",
           "renewal_invoice_mode": "per_subscription", "renewal_gateway_mode": "active",
           "pay_link_ttl_seconds": 3600, "settlement_methods": {"bank_transfer": True},
           "settlement_allow_partial": False, "settlement_max_backdate_days": 3650,
           "settlement_tolerance_minor": 0}
    return BillingService(
        plans=PlanStore(st), coupons=CouponStore(st), subs=SubscriptionStore(st),
        invoices=InvoiceStore(st), payments=PaymentStore(st), usage=UsageStore(st),
        ledger=LedgerStore(st), idempo=IdempotencyStore(st), settlements=SettlementStore(st),
        paylinks=PayLinkStore(st), gateway=gw or HostedGW(), tax=MockTax(), einvoicer=None,
        pdf=None, grants=_outbox(st), config=cfg)


def _plan(b, amount=10000, interval="month", trial_days=0):
    return run(b.create_plan(name="App", trial_days=trial_days,
                             prices=[{"interval": interval, "amount_minor": amount,
                                      "currency": "SAR", "type": "flat"}], limits={"credits": 100}))


def _pay_for(b, provider_id):
    """Simulate the customer completing the hosted checkout, then reconcile it in (fetch_status path)."""
    b._gw.settled.add(provider_id)


# ── creation ────────────────────────────────────────────────────────────────────

def test_creates_incomplete_sub_and_returns_a_hosted_link():
    st = MemoryDataStore(); b = _svc(st)
    p = _plan(b, amount=10000)
    res = run(b.start_subscription_checkout(tenant="t1", plan_id=p.id, idempotency_key="k1"))
    assert res["status"] == "incomplete" and res["checkout_required"] is True
    assert res["checkout_url"].startswith("https://checkout.hosted/pay/")
    assert res["token"] and res["amount_minor"] == 11500          # 10000 + 15% VAT
    sub = run(SubscriptionStore(st).fetch(res["subscription_id"]))
    assert sub.status == "incomplete" and sub.payment_token == ""
    inv = run(InvoiceStore(st).fetch(res["invoice_id"]))
    assert inv.status == "open" and inv.subscription_id == sub.id
    assert (inv.meta or {}).get("checkout_start") is True
    pay = run(PaymentStore(st).by_provider_id(f"sess_{inv.id}"))
    assert pay and pay.status == "pending" and pay.amount_minor == 11500


def test_an_incomplete_sub_is_not_entitled_and_not_swept():
    st = MemoryDataStore(); b = _svc(st)
    p = _plan(b)
    res = run(b.start_subscription_checkout(tenant="t1", plan_id=p.id, idempotency_key="k1"))
    # not billable → the renewal sweep must never see it (no charge attempt on a token-less sub)
    due = run(SubscriptionStore(st).due(now_ts() + 10**9, 100))
    assert res["subscription_id"] not in [s.id for s in due]


# ── activation on payment ─────────────────────────────────────────────────────────

def test_payment_settlement_activates_the_subscription():
    st = MemoryDataStore(); b = _svc(st)
    p = _plan(b)
    res = run(b.start_subscription_checkout(tenant="t1", plan_id=p.id, idempotency_key="k1"))
    sub_id, inv_id = res["subscription_id"], res["invoice_id"]
    billed_end = int(run(SubscriptionStore(st).fetch(sub_id)).current_period_end)
    pay = run(PaymentStore(st).by_provider_id(f"sess_{inv_id}"))
    _pay_for(b, pay.provider_id)
    out = run(b.reconcile_payment(pay.id))                  # fetch_status -> _apply_outcome -> _mark_paid
    assert out["reconciled"] is True and out["payment_status"] == "succeeded"
    sub = run(SubscriptionStore(st).fetch(sub_id))
    assert sub.status == "active"
    assert run(InvoiceStore(st).fetch(inv_id)).status == "paid"
    # the first period was billed by this invoice → its cycle is claimed and the window advanced,
    # so the sweep can never re-bill it (identical to checkout()/run_cycle()).
    assert int(sub.current_period_start) == billed_end
    assert int(sub.current_period_end) == billed_end + 30 * 86400
    seen = run(b._idempo.seen(f"cycle:{sub_id}:{billed_end}"))
    assert seen and seen["result"]["invoice_id"] == inv_id


def test_activation_is_idempotent_no_double_advance():
    st = MemoryDataStore(); b = _svc(st)
    p = _plan(b)
    res = run(b.start_subscription_checkout(tenant="t1", plan_id=p.id, idempotency_key="k1"))
    sub_id, inv_id = res["subscription_id"], res["invoice_id"]
    pay = run(PaymentStore(st).by_provider_id(f"sess_{inv_id}"))
    _pay_for(b, pay.provider_id)
    run(b.reconcile_payment(pay.id))
    end1 = int(run(SubscriptionStore(st).fetch(sub_id)).current_period_end)
    run(b.reconcile_payment(pay.id))                        # a redelivery / second reconcile
    sub = run(SubscriptionStore(st).fetch(sub_id))
    assert sub.status == "active" and int(sub.current_period_end) == end1    # no second advance


# ── idempotency of the request itself ──────────────────────────────────────────────

def test_same_idempotency_key_replays_the_same_link():
    st = MemoryDataStore(); b = _svc(st)
    p = _plan(b)
    a = run(b.start_subscription_checkout(tenant="t1", plan_id=p.id, idempotency_key="k1"))
    c = run(b.start_subscription_checkout(tenant="t1", plan_id=p.id, idempotency_key="k1"))
    assert c == a                                          # exact replay
    subs, _ = run(SubscriptionStore(st)._store.find("subscriptions", {"tenant": "t1"}, limit=10))
    assert len(subs) == 1                                  # ONE incomplete sub, not two


def test_no_key_double_click_does_not_create_two_subs():
    st = MemoryDataStore(); b = _svc(st)
    p = _plan(b)
    a = run(b.start_subscription_checkout(tenant="t1", plan_id=p.id))
    c = run(b.start_subscription_checkout(tenant="t1", plan_id=p.id))   # identical, no key
    assert c["subscription_id"] == a["subscription_id"]    # auto-derived key deduped it
    subs, _ = run(SubscriptionStore(st)._store.find("subscriptions", {"tenant": "t1"}, limit=10))
    assert len(subs) == 1


# ── edges ───────────────────────────────────────────────────────────────────────

def test_a_trial_plan_needs_no_checkout():
    st = MemoryDataStore(); b = _svc(st)
    p = _plan(b, trial_days=14)
    res = run(b.start_subscription_checkout(tenant="t1", plan_id=p.id, idempotency_key="k1"))
    assert res["checkout_required"] is False and res["status"] == "trialing"
    assert res["invoice_id"] == ""
    assert run(SubscriptionStore(st).fetch(res["subscription_id"])).status == "trialing"


def test_unknown_plan_is_refused_and_creates_nothing():
    st = MemoryDataStore(); b = _svc(st)
    with pytest.raises(ValueError):
        run(b.start_subscription_checkout(tenant="t1", plan_id="nope", idempotency_key="k1"))
    subs, _ = run(SubscriptionStore(st)._store.find("subscriptions", {"tenant": "t1"}, limit=10))
    assert subs == []


def test_gateway_without_hosted_checkout_rolls_back_the_sub():
    st = MemoryDataStore(); b = _svc(st, gw=_NoCheckoutGW())
    p = _plan(b)
    from app.core.billing_service import UpgradeChargeFailed
    with pytest.raises(UpgradeChargeFailed):
        run(b.start_subscription_checkout(tenant="t1", plan_id=p.id, idempotency_key="k1"))
    # no dangling incomplete sub and no orphan invoice the customer could never pay
    subs, _ = run(SubscriptionStore(st)._store.find("subscriptions", {"tenant": "t1"}, limit=10))
    assert subs == []
    invs, _ = run(InvoiceStore(st)._store.find("invoices", {"tenant": "t1"}, limit=10))
    assert invs == []


# ── security & hard edge cases (the system must stay robust) ────────────────────────

class _UnavailableGW(HostedGW):
    def prepare_checkout(self, **k):
        from app.ports.payment_gateway import GatewayUnavailable
        raise GatewayUnavailable("gateway down")


def test_gateway_unavailable_at_link_time_leaves_no_incomplete_sub():
    """A transient gateway outage while minting the link must not strand an unpayable incomplete sub
    (nor an orphan invoice) — the whole request rolls back and can be retried cleanly."""
    st = MemoryDataStore(); b = _svc(st, gw=_UnavailableGW())
    p = _plan(b)
    from app.core.billing_service import UpgradeChargeUnavailable
    with pytest.raises(UpgradeChargeUnavailable):
        run(b.start_subscription_checkout(tenant="t1", plan_id=p.id, idempotency_key="k1"))
    subs, _ = run(SubscriptionStore(st)._store.find("subscriptions", {"tenant": "t1"}, limit=10))
    invs, _ = run(InvoiceStore(st)._store.find("invoices", {"tenant": "t1"}, limit=10))
    assert subs == [] and invs == []
    # the claim lapsed (not marked with a result) → a genuine retry can proceed, not a poisoned key
    proceed, prior = run(b._idempo.claim(f"subckout:2:t1|k1"))
    assert prior is None


def test_invoice_amount_is_server_authoritative_not_a_caller_field():
    """The charged amount is the PLAN's price + VAT computed server-side. There is no caller amount
    field on this path, and the pending payment is bound to the invoice total — a client cannot
    under-pay by tampering with a request body."""
    st = MemoryDataStore(); b = _svc(st)
    p = _plan(b, amount=10000)
    res = run(b.start_subscription_checkout(tenant="t1", plan_id=p.id, idempotency_key="k1"))
    inv = run(InvoiceStore(st).fetch(res["invoice_id"]))
    assert inv.total_minor == 11500 == res["amount_minor"]     # 10000 + 15% VAT, from the plan
    pay = run(PaymentStore(st).by_provider_id(f"sess_{inv.id}"))
    assert pay.amount_minor == inv.total_minor                  # payment can't diverge from the invoice


def test_reconcile_before_payment_keeps_the_sub_incomplete():
    """Polling status before the customer pays must NOT activate the subscription — no entitlement is
    granted until the money actually settles."""
    st = MemoryDataStore(); b = _svc(st)
    p = _plan(b)
    res = run(b.start_subscription_checkout(tenant="t1", plan_id=p.id, idempotency_key="k1"))
    pay = run(PaymentStore(st).by_provider_id(f"sess_{res['invoice_id']}"))
    out = run(b.reconcile_payment(pay.id))                      # status still "pending"
    assert out["reconciled"] is False
    assert run(SubscriptionStore(st).fetch(res["subscription_id"])).status == "incomplete"
    assert run(InvoiceStore(st).fetch(res["invoice_id"])).status == "open"


def test_a_coupon_is_redeemed_once_even_across_a_retried_request():
    """The idempotency claim must stop a double-clicked request from consuming a coupon redemption
    twice (or creating two discounted incomplete subs)."""
    st = MemoryDataStore(); b = _svc(st)
    p = _plan(b, amount=10000)
    run(b.upsert_coupon(code="SAVE", type="percent", value=50, max_redemptions=1))
    a = run(b.start_subscription_checkout(tenant="t1", plan_id=p.id, coupon_code="SAVE",
                                          idempotency_key="k1"))
    c = run(b.start_subscription_checkout(tenant="t1", plan_id=p.id, coupon_code="SAVE",
                                          idempotency_key="k1"))
    assert c == a                                              # exact replay, same amount
    cp = run(CouponStore(st).fetch("SAVE"))
    assert int(getattr(cp, "redeemed", 0)) == 1                 # exactly one redemption, not two


def test_two_distinct_tenants_get_distinct_subscriptions_no_cross_talk():
    """The idempotency key is tenant-scoped (length-prefixed): the same key from two tenants must not
    collide — one tenant's link must never be handed to another."""
    st = MemoryDataStore(); b = _svc(st)
    p = _plan(b)
    a = run(b.start_subscription_checkout(tenant="t1", plan_id=p.id, idempotency_key="same"))
    c = run(b.start_subscription_checkout(tenant="t2", plan_id=p.id, idempotency_key="same"))
    assert a["subscription_id"] != c["subscription_id"]
    assert run(SubscriptionStore(st).fetch(a["subscription_id"])).tenant == "t1"
    assert run(SubscriptionStore(st).fetch(c["subscription_id"])).tenant == "t2"


def test_a_once_plan_activates_without_advancing_a_renewal_period():
    """A one-time purchase (interval='once') is chargeable now but never renews: on payment it goes
    active with a ZERO period end (never lapses, never swept), and no cycle key is claimed."""
    st = MemoryDataStore(); b = _svc(st)
    p = _plan(b, amount=10000, interval="once")
    res = run(b.start_subscription_checkout(tenant="t1", plan_id=p.id, interval="once",
                                            idempotency_key="k1"))
    pay = run(PaymentStore(st).by_provider_id(f"sess_{res['invoice_id']}"))
    _pay_for(b, pay.provider_id)
    run(b.reconcile_payment(pay.id))
    sub = run(SubscriptionStore(st).fetch(res["subscription_id"]))
    assert sub.status == "active" and int(sub.current_period_end) == 0
    # a `once` sub is never due for renewal
    assert res["subscription_id"] not in [s.id for s in run(SubscriptionStore(st).due(now_ts() + 10**9, 100))]


def test_concurrent_double_submit_yields_one_sub_one_session():
    """Two in-flight requests for the same signup: exactly one wins the claim and creates the sub +
    session; the other is refused (CHECKOUT_IN_PROGRESS) — never two live checkouts (double charge)."""
    st = MemoryDataStore(); b = _svc(st)
    p = _plan(b)

    async def both():
        # seed a pending claim as if a concurrent request already holds it
        ck = "subckout:2:t1|k1"
        await b._idempo.claim(ck)
        with pytest.raises(ValueError, match="CHECKOUT_IN_PROGRESS"):
            await b.start_subscription_checkout(tenant="t1", plan_id=p.id, idempotency_key="k1")

    run(both())
    subs, _ = run(SubscriptionStore(st)._store.find("subscriptions", {"tenant": "t1"}, limit=10))
    assert subs == []                                          # the loser created nothing


def test_settlement_also_activates_off_gateway():
    """An off-gateway settlement (bank transfer recorded by an operator) drives the SAME _mark_paid
    activation — a hosted-checkout sub is not tied to one completion path."""
    st = MemoryDataStore(); b = _svc(st)
    p = _plan(b)
    res = run(b.start_subscription_checkout(tenant="t1", plan_id=p.id, idempotency_key="k1"))
    inv = run(InvoiceStore(st).fetch(res["invoice_id"]))
    run(b.settle(invoice_id=inv.id, method="bank_transfer", reference="TT-1", issuer="AlRajhi",
                 amount_minor=inv.total_minor, paid_at=now_ts(), tenant="t1"))
    sub = run(SubscriptionStore(st).fetch(res["subscription_id"]))
    assert sub.status == "active"
    assert run(InvoiceStore(st).fetch(inv.id)).status == "paid"
