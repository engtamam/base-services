"""payments-service core conduit — create link, redirect, verified-webhook apply, idempotency,
amount-mismatch refusal, reconcile, and fail-closed for an unconfigured tenant. Uses the console
gateway + in-memory infra so the whole money path is exercised with no real gateway."""
from __future__ import annotations

import asyncio
import json

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.event_bus.memory import MemoryEventBus
from app.core.merchant_credentials import (DRIVER, WEBHOOK_SECRET, MerchantGateways,
                                           merchant_secret_store)
from app.core.payment_requests import PaymentRequestStore
from app.core.payments import (PaymentNotConfigured, PaymentService, TOPIC_COMPLETED)
from app.ports.event_bus import Event
from app.ports.payment_gateway import PaymentOutcome

KEK = "k" * 40
SECRET = "console-secret-xyz"
ALLOWED = frozenset({"pay.example.test"})


def run(c):
    return asyncio.run(c)


async def _svc_with_events():
    store = MemoryDataStore()
    bus = MemoryEventBus()
    events: list[Event] = []

    async def _collect(ev):
        events.append(ev)

    await bus.subscribe(TOPIC_COMPLETED, _collect)
    await bus.subscribe("payment.failed", _collect)

    secrets = merchant_secret_store(store, KEK)
    gateways = MerchantGateways(secrets, console_link_base="https://pay.example.test/checkout", allow_console=True)
    svc = PaymentService(
        requests=PaymentRequestStore(store), gateways=gateways, event_bus=bus,
        redirect_base_url="https://conduit.example", allowed_hosts=ALLOWED, link_ttl_seconds=3600)
    return svc, secrets, events


async def _configure_console(secrets, tenant="acme"):
    await secrets.set(tenant=tenant, name=DRIVER, value="console")
    await secrets.set(tenant=tenant, name=WEBHOOK_SECRET, value=SECRET)


def _webhook_outcome(row, *, status="succeeded", amount=None, currency="SAR", event_id="evt1"):
    return PaymentOutcome(
        status=status, provider="console", provider_ref=row["provider_ref"],
        amount_minor=amount if amount is not None else row["amount_minor"],
        currency=currency, event_id=event_id, verified=True)


def test_create_link_returns_pending_request_and_redirector_url():
    async def go():
        svc, secrets, _ = await _svc_with_events()
        await _configure_console(secrets)
        link = await svc.create_link(tenant="acme", reference="order-1",
                                     amount_minor=15000, currency="SAR")
        assert link.request_id and link.provider == "console"
        assert link.pay_url == f"https://conduit.example/api/v1/payments/pay/{link.request_id}"
        assert link.redirect_url.startswith("https://pay.example.test/checkout/")
        # redirector resolves to the gateway host (allow-listed)
        target = await svc.redirect_target(link.request_id)
        assert target == link.redirect_url
    run(go())


def test_unconfigured_tenant_fails_closed():
    async def go():
        svc, _, _ = await _svc_with_events()
        with pytest.raises(PaymentNotConfigured):
            await svc.create_link(tenant="nobody", reference="o1", amount_minor=100, currency="SAR")
    run(go())


def test_console_gateway_is_not_selectable_in_production():
    """The fake console gateway (marks paid with no real money) must fail closed in production. With
    allow_console=False a tenant that has 'console' selected resolves to the null gateway and is
    reported unconfigured — never a live-looking checkout that collects nothing."""
    async def go():
        store = MemoryDataStore()
        secrets = merchant_secret_store(store, KEK)
        await _configure_console(secrets)
        prod = MerchantGateways(secrets, console_link_base="https://x/checkout")  # allow_console=False
        assert await prod.configured("acme") is False
        gw = await prod.gateway_for("acme")
        assert gw.driver == "null"
        # But outside production the same tenant resolves to the console double.
        dev = MerchantGateways(secrets, console_link_base="https://x/checkout", allow_console=True)
        assert await dev.configured("acme") is True
        assert (await dev.gateway_for("acme")).driver == "console"
    run(go())


def test_create_is_idempotent_by_reference():
    async def go():
        svc, secrets, _ = await _svc_with_events()
        await _configure_console(secrets)
        a = await svc.create_link(tenant="acme", reference="order-9", amount_minor=100, currency="SAR")
        b = await svc.create_link(tenant="acme", reference="order-9", amount_minor=100, currency="SAR")
        assert a.request_id == b.request_id          # no second charge/link for the same order
    run(go())


def test_verified_webhook_completes_once_and_emits_one_event():
    async def go():
        svc, secrets, events = await _svc_with_events()
        await _configure_console(secrets)
        link = await svc.create_link(tenant="acme", reference="o2", amount_minor=15000, currency="SAR")
        row = await svc._req.get(link.request_id)
        out = _webhook_outcome(row)
        updated = await svc.apply_outcome(tenant="acme", outcome=out)
        assert updated["status"] == "completed"
        # replay: same outcome again -> no second transition, no second event
        again = await svc.apply_outcome(tenant="acme", outcome=out)
        assert again["status"] == "completed"
        assert len([e for e in events if e.type == TOPIC_COMPLETED]) == 1
    run(go())


def test_amount_mismatch_is_refused_not_completed():
    async def go():
        svc, secrets, events = await _svc_with_events()
        await _configure_console(secrets)
        link = await svc.create_link(tenant="acme", reference="o3", amount_minor=15000, currency="SAR")
        row = await svc._req.get(link.request_id)
        out = _webhook_outcome(row, amount=9900)     # gateway reports a different sum
        updated = await svc.apply_outcome(tenant="acme", outcome=out)
        assert updated["status"] == "failed" and updated["amount_mismatch"] is True
        assert not any(e.type == TOPIC_COMPLETED for e in events)
    run(go())


def test_unverified_webhook_does_nothing():
    async def go():
        svc, secrets, events = await _svc_with_events()
        await _configure_console(secrets)
        link = await svc.create_link(tenant="acme", reference="o4", amount_minor=100, currency="SAR")
        row = await svc._req.get(link.request_id)
        out = _webhook_outcome(row)
        out.verified = False
        assert await svc.apply_outcome(tenant="acme", outcome=out) is None
        assert (await svc._req.get(link.request_id))["status"] == "pending"
        assert not events
    run(go())


def test_cross_tenant_webhook_cannot_resolve_another_tenants_request():
    async def go():
        svc, secrets, _ = await _svc_with_events()
        await _configure_console(secrets, tenant="acme")
        link = await svc.create_link(tenant="acme", reference="o5", amount_minor=100, currency="SAR")
        row = await svc._req.get(link.request_id)
        # a callback arriving under a DIFFERENT tenant must not settle acme's request
        out = _webhook_outcome(row)
        assert await svc.apply_outcome(tenant="evil", outcome=out) is None
        assert (await svc._req.get(link.request_id))["status"] == "pending"
    run(go())


def test_handle_webhook_end_to_end_through_console_gateway():
    async def go():
        svc, secrets, events = await _svc_with_events()
        await _configure_console(secrets)
        link = await svc.create_link(tenant="acme", reference="o6", amount_minor=15000, currency="SAR")
        row = await svc._req.get(link.request_id)
        body = json.dumps({"status": "succeeded", "provider_ref": row["provider_ref"],
                           "amount_minor": 15000, "currency": "SAR", "event_id": "evt-1",
                           "secret": SECRET}).encode()
        updated = await svc.handle_webhook(tenant="acme", headers={}, body=body)
        assert updated["status"] == "completed"
        # a body with the wrong secret is unverified -> ignored (apply_outcome returns None), and it
        # must NOT complete a second time. (The prior 'is not None or True' always passed and measured
        # nothing — a regression that ACCEPTED the forged body would have slipped through.)
        bad = json.dumps({"status": "succeeded", "provider_ref": row["provider_ref"],
                          "amount_minor": 15000, "secret": "wrong"}).encode()
        assert await svc.handle_webhook(tenant="acme", headers={}, body=bad) is None
        assert len([e for e in events if e.type == TOPIC_COMPLETED]) == 1
    run(go())


def test_concurrent_webhooks_complete_exactly_once():
    async def go():
        svc, secrets, events = await _svc_with_events()
        await _configure_console(secrets)
        link = await svc.create_link(tenant="acme", reference="o-race", amount_minor=15000,
                                     currency="SAR")
        row = await svc._req.get(link.request_id)
        out = _webhook_outcome(row)
        # two verified callbacks for the same request race on the terminal transition
        results = await asyncio.gather(
            svc.apply_outcome(tenant="acme", outcome=_webhook_outcome(row, event_id="a")),
            svc.apply_outcome(tenant="acme", outcome=_webhook_outcome(row, event_id="b")))
        assert all(r["status"] == "completed" for r in results)
        assert len([e for e in events if e.type == TOPIC_COMPLETED]) == 1   # ONE event only
    run(go())


def test_event_and_stored_row_never_carry_the_credential_or_link():
    async def go():
        svc, secrets, events = await _svc_with_events()
        await _configure_console(secrets)
        link = await svc.create_link(tenant="acme", reference="o-red", amount_minor=100,
                                     currency="SAR")
        row = await svc._req.get(link.request_id)
        await svc.apply_outcome(tenant="acme", outcome=_webhook_outcome(row))
        blob = json.dumps([e.payload for e in events])
        assert SECRET not in blob                       # the webhook secret never leaks into an event
        assert "redirect_url" not in blob               # nor the gateway link
    run(go())


def test_sealed_row_stores_ciphertext_not_plaintext():
    async def go():
        store = MemoryDataStore()
        secrets = merchant_secret_store(store, KEK)
        await secrets.set(tenant="acme", name="MERCHANT_API_KEY", value="+966500000000")
        # the raw stored row must not contain the plaintext credential anywhere
        raw = await store.get("merchant_secrets", "acme|MERCHANT_API_KEY")
        assert "+966500000000" not in json.dumps(raw)
        assert raw["sealed"].startswith("v1:gcm:")
        # but an in-process read returns the true value
        assert await secrets.get(tenant="acme", name="MERCHANT_API_KEY") == "+966500000000"
    run(go())


def test_merchant_credentials_are_tenant_isolated():
    async def go():
        store = MemoryDataStore()
        secrets = merchant_secret_store(store, KEK)
        await secrets.set(tenant="shopA", name="MERCHANT_API_KEY", value="A-key")
        await secrets.set(tenant="shopB", name="MERCHANT_API_KEY", value="B-key")
        assert await secrets.get(tenant="shopA", name="MERCHANT_API_KEY") == "A-key"
        assert await secrets.get(tenant="shopB", name="MERCHANT_API_KEY") == "B-key"
        # a tenant with nothing set inherits NO other shop's key (no cross-tenant fallback to a peer)
        assert await secrets.get(tenant="shopC", name="MERCHANT_API_KEY", fallback="none") == "none"
    run(go())


def test_C1_concurrent_create_mints_one_link_no_double_charge():
    """C1 fix: two concurrent create_link for the SAME (tenant, reference) must mint exactly ONE
    gateway bill / one request, never two — the atomic claim guarantees a single winner."""
    async def go():
        svc, secrets, _ = await _svc_with_events()
        await _configure_console(secrets)
        a, b = await asyncio.gather(
            svc.create_link(tenant="acme", reference="order-race", amount_minor=15000, currency="SAR"),
            svc.create_link(tenant="acme", reference="order-race", amount_minor=15000, currency="SAR"))
        assert a.request_id == b.request_id                      # both callers got the SAME request
        rows, _c = await svc._req._s.find(
            "payment_requests", {"tenant": "acme", "reference": "order-race"}, limit=10)
        assert len(rows) == 1                                    # exactly ONE request row exists
    run(go())


def test_C1_gateway_failure_releases_claim_so_retry_can_succeed():
    """A failed mint must not permanently block the order: the claim is released, so a later create
    for the same reference can succeed once the gateway works."""
    async def go():
        store = MemoryDataStore()
        bus = MemoryEventBus()
        secrets = merchant_secret_store(store, KEK)
        # Point the console at an empty link_base so the first attempt fails (no redirect_url)? The
        # console always returns ok; instead simulate failure by configuring NO driver first, then fix.
        gateways = MerchantGateways(secrets, console_link_base="https://pay.example.test/checkout", allow_console=True)
        svc = PaymentService(requests=PaymentRequestStore(store), gateways=gateways, event_bus=bus,
                             redirect_base_url="https://c.example", allowed_hosts=ALLOWED,
                             link_ttl_seconds=3600)
        # first attempt: unconfigured -> PaymentNotConfigured, claim must be released
        with pytest.raises(PaymentNotConfigured):
            await svc.create_link(tenant="acme", reference="o-retry", amount_minor=100, currency="SAR")
        assert await svc._req.get_claim(tenant="acme", reference="o-retry") is None
        # now configure and retry -> succeeds
        await secrets.set(tenant="acme", name=DRIVER, value="console")
        link = await svc.create_link(tenant="acme", reference="o-retry", amount_minor=100, currency="SAR")
        assert link.request_id
    run(go())
