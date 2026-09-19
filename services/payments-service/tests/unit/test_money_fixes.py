"""Regression tests for the checkout-conduit money/correctness fixes (audit H2, H3, M2, M4, M5, I4).

Each test FAILS on the pre-fix code and passes on the fixed code. They exercise the core seam
(apply_outcome / handle_webhook / redirect_target) with the console gateway + in-memory infra, so no
real gateway or network is touched."""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.event_bus.memory import MemoryEventBus
from app.core.merchant_credentials import (DRIVER, WEBHOOK_SECRET, MerchantGateways,
                                           merchant_secret_store)
from app.core.payment_requests import COMPLETED, EXPIRED, PaymentRequestStore
from app.core.payments import PaymentService, PaymentsDisabled, TOPIC_COMPLETED, _peek_provider_ref
from app.ports.event_bus import Event
from app.ports.payment_gateway import CANCELLED, FAILED, PaymentOutcome

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


def _success(row, *, amount=None, currency="SAR", event_id="evt1"):
    return PaymentOutcome(
        status="succeeded", provider="console", provider_ref=row["provider_ref"],
        amount_minor=amount if amount is not None else row["amount_minor"],
        currency=currency, event_id=event_id, verified=True)


# ── H2: a paid order that got marked EXPIRED must still complete ───────────────────
def test_H2_expired_then_paid_completes_and_emits_one_event():
    async def go():
        svc, secrets, events = await _svc_with_events()
        await _configure_console(secrets)
        link = await svc.create_link(tenant="acme", reference="o-exp", amount_minor=15000,
                                     currency="SAR")
        row = await svc._req.get(link.request_id)
        # our TTL elapsed and a redirect hit marked it expired BEFORE the real webhook lands
        won, expired = await svc._req.mark_expired(row)
        assert won and expired["status"] == EXPIRED
        # the verified success arrives on the already-expired row: it must supersede, not be dropped
        updated = await svc.apply_outcome(tenant="acme", outcome=_success(row))
        assert updated["status"] == COMPLETED
        assert len([e for e in events if e.type == TOPIC_COMPLETED]) == 1
    run(go())


# ── M4: a success must supersede a prior non-success terminal (failed-then-paid) ───
def test_M4_failed_then_paid_completes():
    async def go():
        svc, secrets, events = await _svc_with_events()
        await _configure_console(secrets)
        link = await svc.create_link(tenant="acme", reference="o-fp", amount_minor=15000,
                                     currency="SAR")
        row = await svc._req.get(link.request_id)
        # a decline lands first (verified, amount matches) → request goes FAILED
        declined = PaymentOutcome(status=FAILED, provider="console",
                                  provider_ref=row["provider_ref"], amount_minor=15000,
                                  currency="SAR", event_id="fail1", verified=True)
        after_fail = await svc.apply_outcome(tenant="acme", outcome=declined)
        assert after_fail["status"] == FAILED
        # the customer then retries on the same link and genuinely pays → success supersedes the fail
        after_pay = await svc.apply_outcome(tenant="acme", outcome=_success(row, event_id="pay1"))
        assert after_pay["status"] == COMPLETED
        assert len([e for e in events if e.type == TOPIC_COMPLETED]) == 1
    run(go())


def test_M4_non_success_never_overwrites_a_completed_row():
    async def go():
        svc, secrets, _ = await _svc_with_events()
        await _configure_console(secrets)
        link = await svc.create_link(tenant="acme", reference="o-cf", amount_minor=100,
                                     currency="SAR")
        row = await svc._req.get(link.request_id)
        assert (await svc.apply_outcome(tenant="acme", outcome=_success(row)))["status"] == COMPLETED
        # a late failure/cancel must NOT rewrite the completed money fact
        late = PaymentOutcome(status=CANCELLED, provider="console", provider_ref=row["provider_ref"],
                              amount_minor=100, currency="SAR", event_id="c1", verified=True)
        assert (await svc.apply_outcome(tenant="acme", outcome=late))["status"] == COMPLETED
    run(go())


# ── H3: a verified success with amount 0/absent is UNVERIFIABLE, not matching ──────
def test_H3_zero_amount_success_is_refused_not_completed():
    async def go():
        svc, secrets, events = await _svc_with_events()
        await _configure_console(secrets)
        link = await svc.create_link(tenant="acme", reference="o-zero", amount_minor=15000,
                                     currency="SAR")
        row = await svc._req.get(link.request_id)
        updated = await svc.apply_outcome(tenant="acme", outcome=_success(row, amount=0))
        assert updated["status"] == FAILED and updated["amount_mismatch"] is True
        assert not any(e.type == TOPIC_COMPLETED for e in events)
    run(go())


def test_a_success_with_no_confirmable_currency_is_refused_not_completed():
    """MEDIUM regression: an absent currency was read as "matches" (SAR/USD/EUR share exponent 2, so
    the bare amount compare cannot tell them apart) — a success in another currency could complete a
    request. A SUCCEEDED with no confirmable currency is now UNVERIFIABLE, refused like a 0 amount."""
    async def go():
        svc, secrets, events = await _svc_with_events()
        await _configure_console(secrets)
        link = await svc.create_link(tenant="acme", reference="o-nocur", amount_minor=15000,
                                     currency="SAR")
        row = await svc._req.get(link.request_id)
        updated = await svc.apply_outcome(tenant="acme", outcome=_success(row, currency=""))
        assert updated["status"] == FAILED and updated["amount_mismatch"] is True
        assert not any(e.type == TOPIC_COMPLETED for e in events)
    run(go())


def test_a_success_in_a_different_currency_is_refused():
    async def go():
        svc, secrets, events = await _svc_with_events()
        await _configure_console(secrets)
        link = await svc.create_link(tenant="acme", reference="o-usd", amount_minor=15000,
                                     currency="SAR")
        row = await svc._req.get(link.request_id)
        updated = await svc.apply_outcome(tenant="acme", outcome=_success(row, currency="USD"))
        assert updated["status"] == FAILED and updated["amount_mismatch"] is True
        assert not any(e.type == TOPIC_COMPLETED for e in events)
    run(go())


def test_H3_matching_amount_still_completes():
    async def go():
        svc, secrets, _ = await _svc_with_events()
        await _configure_console(secrets)
        link = await svc.create_link(tenant="acme", reference="o-ok", amount_minor=15000,
                                     currency="SAR")
        row = await svc._req.get(link.request_id)
        updated = await svc.apply_outcome(tenant="acme", outcome=_success(row))
        assert updated["status"] == COMPLETED and updated["amount_mismatch"] is False
    run(go())


# ── M5: redirect scheme must be https, not just an allow-listed host ───────────────
def test_M5_http_redirect_target_is_refused():
    async def go():
        svc, secrets, _ = await _svc_with_events()
        await _configure_console(secrets)
        link = await svc.create_link(tenant="acme", reference="o-dg", amount_minor=100,
                                     currency="SAR")
        row = await svc._req.get(link.request_id)
        # tamper the stored redirect_url to an http:// downgrade on the SAME allow-listed host
        downgraded = {k: v for k, v in row.items() if k != "version"}
        downgraded["redirect_url"] = "http://pay.example.test/checkout/x"
        assert await svc._req._s.cas("payment_requests", row["id"], row["version"], downgraded)
        with pytest.raises(PaymentsDisabled):
            await svc.redirect_target(link.request_id)
    run(go())


def test_M5_host_allowed_requires_https():
    async def go():
        svc, _, _ = await _svc_with_events()
        assert svc._host_allowed("https://pay.example.test/x") is True
        assert svc._host_allowed("http://pay.example.test/x") is False   # scheme downgrade blocked
        assert svc._host_allowed("https://evil.test/x") is False          # host not allow-listed
    run(go())


# ── M2: a webhook for an unknown provider_ref makes NO outbound gateway call ───────
class _CountingGateway:
    driver = "console"

    def __init__(self):
        self.verify_calls = 0

    def verify_webhook(self, *, headers, body):
        self.verify_calls += 1
        return PaymentOutcome(status="pending", provider="console", verified=False)


class _FixedGateways:
    def __init__(self, gw):
        self._gw = gw

    async def gateway_for(self, tenant):
        return self._gw


def _svc_with_gateway(gw):
    store = MemoryDataStore()
    return PaymentService(
        requests=PaymentRequestStore(store), gateways=_FixedGateways(gw),
        event_bus=MemoryEventBus(), redirect_base_url="https://conduit.example",
        allowed_hosts=ALLOWED, link_ttl_seconds=3600)


def test_M2_junk_provider_ref_skips_the_outbound_verify():
    async def go():
        gw = _CountingGateway()
        svc = _svc_with_gateway(gw)
        # body plainly names a ref this tenant has NO request for → short-circuit, no verify call
        body = b'{"status": "succeeded", "provider_ref": "ghost", "secret": "x"}'
        assert await svc.handle_webhook(tenant="acme", headers={}, body=body) is None
        assert gw.verify_calls == 0
    run(go())


def test_M2_body_without_a_ref_still_verifies():
    async def go():
        gw = _CountingGateway()
        svc = _svc_with_gateway(gw)
        # a body exposing no resolvable ref must fall through to full verification (never weakened)
        assert await svc.handle_webhook(tenant="acme", headers={}, body=b"not-json") is None
        assert gw.verify_calls == 1
    run(go())


def test_peek_provider_ref_shapes():
    assert _peek_provider_ref(b'{"provider_ref": "abc"}') == "abc"
    assert _peek_provider_ref(b'{"transactionDetails": {"transactionId": "tx9"}}') == "tx9"
    assert _peek_provider_ref(b'{"transactionId": "t1"}') == "t1"
    assert _peek_provider_ref(b"not-json") == ""
    assert _peek_provider_ref(b"") == ""


# ── I4: a public get() so routes need not reach svc._req ───────────────────────────
def test_I4_public_get_returns_the_request_row():
    async def go():
        svc, secrets, _ = await _svc_with_events()
        await _configure_console(secrets)
        link = await svc.create_link(tenant="acme", reference="o-get", amount_minor=100,
                                     currency="SAR")
        got = await svc.get(link.request_id)
        assert got is not None and got["id"] == link.request_id
        assert await svc.get("no-such-id") is None
    run(go())
