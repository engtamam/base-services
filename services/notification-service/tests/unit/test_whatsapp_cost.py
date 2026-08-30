"""WhatsApp cost tracking — record the REAL billable units of a provider-billed send, expose them per
tenant/period, and record NOTHING for a Meta-direct send the shop pays for itself.

The load-bearing behaviours, each pinned here:
  * only a send through a PROVIDER-BILLED driver (360dialog) is metered; a `meta` send is invisible;
  * a message is counted ONCE even though Meta fires (and retries) several status events for it;
  * the recorded unit is the observed category / country / pricing_model / billable — never money;
  * the internal read route aggregates one tenant's units for one period, token-guarded.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import pytest
from starlette.testclient import TestClient

from app.config import get_settings
from app.core.e164_country_codes import country_calling_code
from app.core.models import Delivery
from app.core.stores import DeliveryStore, WhatsAppCostStore, whatsapp_cost_period

SECRET = "app-secret-xyz"
VERIFY = "verify-nonce-123"
TOKEN = "internal-token-abcdefghijklmnopqrstuvwxyz"   # ≥32 chars (Settings enforces it)


# ── the country derivation (prefix-free ⇒ a miss is "", never a wrong country) ────
@pytest.mark.parametrize("msisdn,code", [
    ("966501234567", "966"), ("+966 50 123 4567", "966"), ("14155550123", "1"),
    ("79261234567", "7"), ("442071234567", "44"), ("201001234567", "20"),
    ("", ""), ("000", ""), ("999", ""),          # 999 is not an assigned code → unknown, not wrong
])
def test_country_calling_code(msisdn, code):
    assert country_calling_code(msisdn) == code


def test_period_is_the_utc_month_of_the_send():
    # 1700000000 = 2023-11-14 UTC
    assert whatsapp_cost_period(1_700_000_000.0) == "2023-11"


# ── the store: idempotency + aggregation ──────────────────────────────────────────
class _MemStore:
    """Minimal in-memory DataStore with the equality-find the aggregation uses."""
    driver = "mem"

    def __init__(self):
        self._c: dict[tuple[str, str], dict] = {}

    async def get(self, collection, key):
        return self._c.get((collection, key))

    async def put(self, collection, key, value):
        self._c[(collection, key)] = dict(value)

    async def find(self, collection, where, *, limit=100, after=None, sort=("created", "desc")):
        rows = [v for (c, _), v in self._c.items() if c == collection
                and all(v.get(k) == val for k, val in where.items())]
        rows.sort(key=lambda r: (r.get(sort[0], 0), r.get("id", "")))
        start = 0
        if after is not None:
            ids = [r["id"] for r in rows]
            start = ids.index(after) + 1 if after in ids else 0
        page = rows[start:start + limit]
        nxt = page[-1]["id"] if len(page) == limit and start + limit < len(rows) else None
        return page, nxt


def test_record_is_idempotent_by_wamid():
    store = WhatsAppCostStore(_MemStore())

    async def go():
        first = await store.record(wamid="wamid.1", tenant="acme", ts=1_700_000_000.0,
                                   category="marketing", country="966", pricing_model="PMP",
                                   billable=True, msg_type="regular")
        # a webhook RETRY of the same message must not double-count
        again = await store.record(wamid="wamid.1", tenant="acme", ts=1_700_000_000.0,
                                   category="marketing", country="966", pricing_model="PMP",
                                   billable=True, msg_type="regular")
        empty = await store.record(wamid="", tenant="acme", ts=1_700_000_000.0, category="x",
                                   country="", pricing_model="", billable=False, msg_type="")
        usage = await store.usage_for_period("acme", "2023-11")
        return first, again, empty, usage

    first, again, empty, usage = asyncio.run(go())
    assert first is True and again is False and empty is False   # counted once, retry+no-wamid refused
    assert usage["total_messages"] == 1 and usage["billable_messages"] == 1
    assert usage["breakdown"] == [
        {"category": "marketing", "country": "966", "pricing_model": "PMP",
         "billable": True, "messages": 1}]


def test_usage_aggregates_by_dimension_and_scopes_to_the_tenant_and_period():
    store = WhatsAppCostStore(_MemStore())

    async def go():
        await store.record(wamid="a", tenant="acme", ts=1_700_000_000.0, category="marketing",
                           country="966", pricing_model="PMP", billable=True, msg_type="regular")
        await store.record(wamid="b", tenant="acme", ts=1_700_000_100.0, category="marketing",
                           country="966", pricing_model="PMP", billable=True, msg_type="regular")
        await store.record(wamid="c", tenant="acme", ts=1_700_000_200.0, category="utility",
                           country="1", pricing_model="PMP", billable=False,
                           msg_type="free_customer_service")
        # another tenant, and another month — must NOT leak into acme/2023-11
        await store.record(wamid="d", tenant="other", ts=1_700_000_000.0, category="marketing",
                           country="966", pricing_model="PMP", billable=True, msg_type="regular")
        await store.record(wamid="e", tenant="acme", ts=1_690_000_000.0, category="marketing",
                           country="966", pricing_model="PMP", billable=True, msg_type="regular")
        return await store.usage_for_period("acme", "2023-11")

    usage = asyncio.run(go())
    assert usage["total_messages"] == 3 and usage["billable_messages"] == 2
    assert {(b["category"], b["country"], b["billable"], b["messages"]) for b in usage["breakdown"]} == {
        ("marketing", "966", True, 2), ("utility", "1", False, 1)}


# ── end to end: the webhook meters only provider-billed sends, the route reports them ──
@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("META_APP_SECRET", SECRET)
    monkeypatch.setenv("WHATSAPP_WEBHOOK_VERIFY_TOKEN", VERIFY)
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", TOKEN)
    get_settings.cache_clear()
    from app.main import create_app
    with TestClient(create_app()) as c:
        yield c
    get_settings.cache_clear()


def _sign(raw: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()


def _seed_delivery(client, *, wamid, provider, tenant, recipient):
    ds = client.app.state.container.datastore
    asyncio.run(DeliveryStore(ds).record(Delivery(
        id=f"d-{wamid}", recipient=recipient, channel="whatsapp", template_key="order_ready",
        status="sent", tenant=tenant, provider=provider, provider_message_id=wamid)))


def _status_webhook(client, *, wamid, pricing):
    payload = {"object": "whatsapp_business_account", "entry": [{"id": "WABA1", "changes": [
        {"field": "messages", "value": {
            "metadata": {"phone_number_id": "PN1"},
            "statuses": [{"id": wamid, "status": "sent", "timestamp": "1700000000",
                          "recipient_id": "966501234567", "pricing": pricing}]}}]}]}
    raw = json.dumps(payload).encode()
    return client.post("/webhooks/whatsapp", content=raw,
                       headers={"X-Hub-Signature-256": _sign(raw)})


def _usage(client, tenant, period="2023-11"):
    return client.get("/internal/whatsapp/channel-usage",
                      params={"tenant": tenant, "period": period},
                      headers={"X-Internal-Token": TOKEN})


def test_a_provider_billed_send_is_metered_and_readable_by_the_internal_route(client):
    _seed_delivery(client, wamid="wamid.d360", provider="threesixtydialog",
                   tenant="acme", recipient="966501234567")
    r = _status_webhook(client, wamid="wamid.d360", pricing={
        "billable": True, "pricing_model": "PMP", "type": "regular", "category": "marketing"})
    assert r.status_code == 200

    got = _usage(client, "acme")
    assert got.status_code == 200
    body = got.json()
    assert body["total_messages"] == 1 and body["billable_messages"] == 1
    assert body["breakdown"] == [{"category": "marketing", "country": "966",
                                  "pricing_model": "PMP", "billable": True, "messages": 1}]


def test_a_meta_direct_send_records_nothing(client):
    # meta-direct: the shop pays Meta on its own account, so it is NOT ours to meter.
    _seed_delivery(client, wamid="wamid.meta", provider="meta",
                   tenant="acme", recipient="966501234567")
    assert _status_webhook(client, wamid="wamid.meta", pricing={
        "billable": True, "pricing_model": "PMP", "type": "regular", "category": "marketing"}
    ).status_code == 200
    assert _usage(client, "acme").json()["total_messages"] == 0


def test_a_status_event_with_no_pricing_object_records_nothing(client):
    # delivered/read events carry no pricing; only the billing-determining event does.
    _seed_delivery(client, wamid="wamid.np", provider="threesixtydialog",
                   tenant="acme", recipient="966501234567")
    payload = {"object": "whatsapp_business_account", "entry": [{"id": "W", "changes": [
        {"field": "messages", "value": {"metadata": {"phone_number_id": "PN1"},
                                        "statuses": [{"id": "wamid.np", "status": "read",
                                                      "timestamp": "1700000000"}]}}]}]}
    raw = json.dumps(payload).encode()
    assert client.post("/webhooks/whatsapp", content=raw,
                       headers={"X-Hub-Signature-256": _sign(raw)}).status_code == 200
    assert _usage(client, "acme").json()["total_messages"] == 0


def test_the_usage_route_requires_the_internal_token(client):
    assert client.get("/internal/whatsapp/channel-usage",
                      params={"tenant": "acme"}).status_code == 401
    assert client.get("/internal/whatsapp/channel-usage", params={"tenant": "acme"},
                      headers={"X-Internal-Token": "wrong"}).status_code == 401
