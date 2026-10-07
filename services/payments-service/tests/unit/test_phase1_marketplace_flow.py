"""Phase 1 — the create_link MARKETPLACE branch end to end (fake provider, real stores).

The security spine of split payments: a tenant can split ONLY to its OWN active sub-merchant destination,
the server computes the split (platform fee = remainder), and every fail-closed path (no provider, no
active sub-merchant, missing/over-total share, wrong mode) refuses BEFORE minting — claim released,
nothing persisted. The marketplace webhook is verified by the marketplace provider (mode-aware).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.event_bus.memory import MemoryEventBus
from app.core.payment_requests import PaymentRequestStore
from app.core.payments import PaymentNotConfigured, PaymentService
from app.core.submerchant import ST_PENDING, SubMerchantStore
from app.ports.payment_gateway import (CAP_CHECKOUT, CAP_ONBOARDING, CAP_PAYOUT, CAP_SPLIT,
                                       CheckoutSession, MODEL_MARKETPLACE, PaymentOutcome)


def run(c):
    return asyncio.run(c)


class _FakeMarketplaceGateway:
    driver = "fake_mkt"
    model = MODEL_MARKETPLACE

    def __init__(self, capabilities=frozenset({CAP_CHECKOUT, CAP_SPLIT, CAP_ONBOARDING, CAP_PAYOUT})):
        self.capabilities = capabilities
        self.onboarding_modes = frozenset()
        self.calls = []

    def prepare_split_checkout(self, *, amount_minor, currency, reference, destinations,
                               return_url="", metadata=None):
        self.calls.append({"amount_minor": amount_minor, "destinations": list(destinations)})
        return CheckoutSession(ok=True, provider=self.driver, provider_ref=f"chg_{reference}",
                               redirect_url="https://pay.fake/x")

    def verify_webhook(self, *, headers, body, stored_currency=""):
        d = json.loads(body.decode())
        return PaymentOutcome(status="succeeded", provider=self.driver, provider_ref=d["id"],
                              amount_minor=int(d["amount_minor"]), currency=d["currency"],
                              event_id=d.get("event_id", ""), verified=True)

    def fetch_status(self, *, provider_ref, currency=""):
        return PaymentOutcome(status="pending", provider=self.driver, provider_ref=provider_ref,
                              currency=currency, verified=True)


class _Resolver:
    def __init__(self, gw):
        self._gw = gw

    async def gateway(self):
        return self._gw

    async def configured(self):
        return self._gw is not None


class _NullConduit:
    """A conduit resolver that never has a gateway — marketplace tests never hit it."""
    async def gateway_for(self, tenant):
        from app.adapters.payment import build_gateway
        return build_gateway("null")

    async def driver_for(self, tenant):
        return ""

    async def configured(self, tenant):
        return False


def _svc(*, marketplace_gw, mode=MODEL_MARKETPLACE, submerchants=None):
    store = MemoryDataStore()
    subs = submerchants if submerchants is not None else SubMerchantStore(store)

    async def sel(_t):
        return mode

    svc = PaymentService(
        requests=PaymentRequestStore(store), gateways=_NullConduit(), event_bus=MemoryEventBus(),
        redirect_base_url="https://c.example", allowed_hosts=frozenset({"pay.fake"}),
        link_ttl_seconds=3600, select_mode=sel,
        marketplace=_Resolver(marketplace_gw) if marketplace_gw is not None else None,
        submerchants=subs)
    return svc, store, subs


# ── happy path ────────────────────────────────────────────────────────────────────
def test_marketplace_split_checkout_mints_and_records_the_split():
    gw = _FakeMarketplaceGateway()
    svc, store, subs = _svc(marketplace_gw=gw)
    run(subs.set(tenant="acme", provider="fake_mkt", destination_id="dst_acme"))
    link = run(svc.create_link(tenant="acme", reference="o1", amount_minor=10000, currency="SAR",
                               metadata={"customerName": "A", "customerEmail": "a@b.c"},
                               merchant_amount_minor=9000))
    assert link.request_id
    # Routed to the tenant's OWN destination; platform keeps the remainder.
    assert gw.calls[0]["destinations"][0].destination_id == "dst_acme"
    assert gw.calls[0]["destinations"][0].amount_minor == 9000
    row = run(PaymentRequestStore(store).get(link.request_id))
    assert row["mode"] == MODEL_MARKETPLACE
    assert row["meta"]["split"]["platform_fee_minor"] == 1000


# ── fail-closed paths: refuse before minting, persist nothing ───────────────────────
def _assert_refused_and_clean(svc, store, **kw):
    with pytest.raises(PaymentNotConfigured):
        run(svc.create_link(tenant="acme", reference="ox", amount_minor=10000, currency="SAR",
                            metadata={"customerName": "A", "customerEmail": "a@b.c"}, **kw))
    assert run(PaymentRequestStore(store).active_by_reference(tenant="acme", reference="ox")) is None


def test_refuse_when_tenant_has_no_submerchant():
    gw = _FakeMarketplaceGateway()
    svc, store, _ = _svc(marketplace_gw=gw)
    _assert_refused_and_clean(svc, store, merchant_amount_minor=9000)
    assert gw.calls == []


def test_refuse_when_submerchant_is_pending():
    gw = _FakeMarketplaceGateway()
    svc, store, subs = _svc(marketplace_gw=gw)
    run(subs.set(tenant="acme", provider="fake_mkt", destination_id="d", status=ST_PENDING))
    _assert_refused_and_clean(svc, store, merchant_amount_minor=9000)


def test_refuse_when_merchant_amount_missing():
    gw = _FakeMarketplaceGateway()
    svc, store, subs = _svc(marketplace_gw=gw)
    run(subs.set(tenant="acme", provider="fake_mkt", destination_id="d"))
    _assert_refused_and_clean(svc, store, merchant_amount_minor=None)


def test_refuse_when_share_exceeds_total():
    gw = _FakeMarketplaceGateway()
    svc, store, subs = _svc(marketplace_gw=gw)
    run(subs.set(tenant="acme", provider="fake_mkt", destination_id="d"))
    _assert_refused_and_clean(svc, store, merchant_amount_minor=10001)   # > 10000 total
    assert gw.calls == []


def test_refuse_when_no_marketplace_provider_wired():
    svc, store, subs = _svc(marketplace_gw=None)
    run(subs.set(tenant="acme", provider="fake_mkt", destination_id="d"))
    _assert_refused_and_clean(svc, store, merchant_amount_minor=9000)


def test_refuse_when_provider_lacks_split_capability():
    gw = _FakeMarketplaceGateway(capabilities=frozenset({CAP_CHECKOUT}))   # no split
    svc, store, subs = _svc(marketplace_gw=gw)
    run(subs.set(tenant="acme", provider="fake_mkt", destination_id="d"))
    _assert_refused_and_clean(svc, store, merchant_amount_minor=9000)


def test_conduit_mode_rejects_a_merchant_amount():
    gw = _FakeMarketplaceGateway()
    svc, store, _ = _svc(marketplace_gw=gw, mode="conduit")
    # merchant_amount only valid in marketplace — a conduit tenant passing it is a bug, refused.
    _assert_refused_and_clean(svc, store, merchant_amount_minor=9000)


def test_refused_marketplace_create_leaves_order_id_reusable():
    gw = _FakeMarketplaceGateway()
    svc, store, subs = _svc(marketplace_gw=gw)
    with pytest.raises(PaymentNotConfigured):               # no sub-merchant yet
        run(svc.create_link(tenant="acme", reference="ord-r", amount_minor=10000, currency="SAR",
                            metadata={"customerName": "A", "customerEmail": "a@b.c"},
                            merchant_amount_minor=9000))
    run(subs.set(tenant="acme", provider="fake_mkt", destination_id="dst"))
    link = run(svc.create_link(tenant="acme", reference="ord-r", amount_minor=10000, currency="SAR",
                               metadata={"customerName": "A", "customerEmail": "a@b.c"},
                               merchant_amount_minor=9000))
    assert link.request_id                                  # claim was released → same id mints now


# ── mode-aware webhook ──────────────────────────────────────────────────────────────
def test_marketplace_webhook_is_verified_by_the_marketplace_provider():
    gw = _FakeMarketplaceGateway()
    svc, store, subs = _svc(marketplace_gw=gw)
    run(subs.set(tenant="acme", provider="fake_mkt", destination_id="dst"))
    link = run(svc.create_link(tenant="acme", reference="o9", amount_minor=10000, currency="SAR",
                               metadata={"customerName": "A", "customerEmail": "a@b.c"},
                               merchant_amount_minor=9000))
    row = run(PaymentRequestStore(store).get(link.request_id))
    body = json.dumps({"id": row["provider_ref"], "amount_minor": 10000, "currency": "SAR",
                       "event_id": "evt1"}).encode()
    applied = run(svc.handle_webhook(tenant="acme", headers={}, body=body))
    assert applied is not None and applied["status"] == "completed"
