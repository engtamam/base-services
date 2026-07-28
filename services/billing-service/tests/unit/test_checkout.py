"""BillingService.prepare_checkout — delegate to the gateway, and say so when a provider can't."""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.billing_service import BillingService
from app.ports.payment_gateway import CheckoutSession
from app.core.stores import (CouponStore, IdempotencyStore, InvoiceStore, LedgerStore,
                             PaymentStore, PlanStore, SettlementStore, SubscriptionStore, UsageStore)


def run(c):
    return asyncio.run(c)


def _svc(gateway):
    st = MemoryDataStore()
    return BillingService(
        plans=PlanStore(st), coupons=CouponStore(st), subs=SubscriptionStore(st),
        invoices=InvoiceStore(st), payments=PaymentStore(st), usage=UsageStore(st),
        ledger=LedgerStore(st), idempo=IdempotencyStore(st), gateway=gateway, tax=None,
        einvoicer=None, pdf=None, settlements=SettlementStore(st),
        config={"currency": "SAR", "trial_days": 0, "vat_rate": "0.15", "reverse_charge": False,
                "grandfather": True, "dunning_base_hours": 24, "dunning_max_attempts": 4,
                "grace_days": 7, "list_limit": 100, "limits_cache_ttl": 300,
                "billable_statuses": ("active",), "max_subscriptions": 200,
                "merge_strategies": {}, "rank_orders": {}})


class _Gateway:
    driver = "fake"

    def prepare_checkout(self, *, amount_minor, currency, reference, return_url="", metadata=None):
        assert currency == "SAR"                 # the service defaulted it from config
        return CheckoutSession(ok=True, provider="fake", id="sess_1",
                               widget={"session_id": "sess_1"})


class _NoCheckoutGateway:
    driver = "null"                              # a provider WITHOUT prepare_checkout


def test_prepare_checkout_delegates_to_a_capable_gateway():
    out = run(_svc(_Gateway()).prepare_checkout(amount_minor=5000, reference="inv_1"))
    assert out["supported"] is True and out["ok"] is True and out["id"] == "sess_1"


def test_a_provider_without_server_side_checkout_says_unsupported():
    out = run(_svc(_NoCheckoutGateway()).prepare_checkout(amount_minor=5000, reference="inv_1"))
    assert out["supported"] is False and out["ok"] is False and out["provider"] == "null"
