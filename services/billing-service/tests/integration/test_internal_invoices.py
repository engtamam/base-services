"""GET /internal/billing/invoices — the durable, idempotent paid-invoice read a peer service keys
money on (events.py's own contract: the at-most-once bus is NOT how a money consequence is delivered).

Security surface under test: internal-token only, tenant-scoped, money-relevant statuses only, and the
same privacy discipline as payment_payload() — no buyer name, no tax identity.
"""
from __future__ import annotations

import asyncio
import os

import pytest
from fastapi.testclient import TestClient

INTERNAL = "t" * 40
IT = {"X-Internal-Token": INTERNAL}


def run(c):
    return asyncio.run(c)


@pytest.fixture(scope="module")
def client():
    from app.config import get_settings
    saved = dict(os.environ)
    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN=INTERNAL, TOKEN_VERIFIER_DRIVER="hs256",
                      TOKEN_HS256_SECRET="s" * 40, RATELIMIT_MAX="100000")
    get_settings.cache_clear()
    try:
        from app.main import create_app
        with TestClient(create_app()) as c:
            yield c
    finally:
        os.environ.clear()
        os.environ.update(saved)
        get_settings.cache_clear()


def _put_invoice(client, **over):
    ds = client.app.state.container.datastore
    inv = {"id": over["id"], "tenant": over.get("tenant", "t1"),
           "subscription_id": over.get("subscription_id", "sub_a"),
           "status": over.get("status", "paid"), "total_minor": over.get("total_minor", 10000),
           "refunded_minor": over.get("refunded_minor", 0), "currency": "SAR",
           "paid_at": over.get("paid_at", ""), "created": over.get("created", over.get("paid_at", "")),
           # fields that must NEVER surface across the internal API:
           "buyer_name": "Alice Al-Otaibi", "buyer_vat": "310000000000003", "number": "INV-000001",
           "lines": [{"description": "Pro plan", "amount_minor": 10000}]}
    inv.update({k: v for k, v in over.items()})
    run(ds.put("invoices", inv["id"], inv))


@pytest.fixture
def seeded(client):
    # t1: three paid across two subscriptions (out of order to prove server-side sorting), one void,
    # one draft (must be invisible). t2: a paid invoice that must never leak to t1.
    _put_invoice(client, id="p2", subscription_id="sub_a", paid_at="2026-03-01T00:00:00+00:00",
                 total_minor=10000)
    _put_invoice(client, id="p1", subscription_id="sub_a", paid_at="2026-02-01T00:00:00+00:00",
                 total_minor=10000)
    _put_invoice(client, id="pb", subscription_id="sub_b", paid_at="2026-02-15T00:00:00+00:00",
                 total_minor=6000, refunded_minor=6000)
    _put_invoice(client, id="v1", status="void", paid_at="2026-01-01T00:00:00+00:00")
    _put_invoice(client, id="d1", status="draft", paid_at="")
    _put_invoice(client, id="other", tenant="t2", paid_at="2026-02-01T00:00:00+00:00")
    return client


def test_lists_paid_oldest_first_with_the_money_fields(seeded):
    r = seeded.get("/internal/billing/invoices?tenant=t1", headers=IT)
    assert r.status_code == 200
    inv = r.json()["invoices"]
    assert [i["id"] for i in inv] == ["p1", "pb", "p2"]              # sorted by paid_at, not insert order
    assert inv[0]["amount_minor"] == 10000 and inv[0]["currency"] == "SAR"
    assert inv[1]["refunded_minor"] == 6000                          # a refund a commission must see


def test_sequence_numbers_distinguish_conversion_from_nth_renewal(seeded):
    inv = seeded.get("/internal/billing/invoices?tenant=t1", headers=IT).json()["invoices"]
    by_id = {i["id"]: i for i in inv}
    assert by_id["p1"]["paid_sequence"] == 1                        # the tenant's FIRST payment = conversion
    assert by_id["p2"]["paid_sequence"] == 3
    # per-subscription: sub_a's p1 is its first payment, p2 its first renewal; sub_b's pb is its first
    assert by_id["p1"]["subscription_paid_sequence"] == 1
    assert by_id["p2"]["subscription_paid_sequence"] == 2
    assert by_id["pb"]["subscription_paid_sequence"] == 1


def test_status_void_is_allowed_for_clawback(seeded):
    inv = seeded.get("/internal/billing/invoices?tenant=t1&status=void", headers=IT).json()["invoices"]
    assert [i["id"] for i in inv] == ["v1"]


def test_a_non_money_status_is_refused_not_a_probe_of_working_state(seeded):
    assert seeded.get("/internal/billing/invoices?tenant=t1&status=draft", headers=IT).status_code == 400
    assert seeded.get("/internal/billing/invoices?tenant=t1&status=open", headers=IT).status_code == 400


def test_no_buyer_identity_or_line_items_ever_cross_the_wire(seeded):
    body = seeded.get("/internal/billing/invoices?tenant=t1", headers=IT).text
    for leaked in ("Alice", "310000000000003", "INV-000001", "buyer_name", "buyer_vat", "lines"):
        assert leaked not in body, f"{leaked!r} leaked across the internal API"


def test_tenant_isolation(seeded):
    ids = [i["id"] for i in seeded.get("/internal/billing/invoices?tenant=t1", headers=IT).json()["invoices"]]
    assert "other" not in ids                                       # t2's invoice never appears for t1


def test_requires_the_internal_token(seeded):
    assert seeded.get("/internal/billing/invoices?tenant=t1").status_code == 401


def test_requires_a_tenant(seeded):
    assert seeded.get("/internal/billing/invoices", headers=IT).status_code in (400, 422)


def test_store_history_reports_truncation(client):
    # The bounded-read guarantee: past the cap, `truncated` is loud, not a silent dropped tail.
    from app.core.stores import InvoiceStore
    ds = client.app.state.container.datastore
    for n in range(3):
        _put_invoice(client, id=f"tr{n}", tenant="ttrunc", paid_at=f"2026-0{n+1}-01T00:00:00+00:00")
    rows, truncated = run(InvoiceStore(ds).history_for_tenant("ttrunc", "paid", limit=2))
    assert len(rows) == 2 and truncated is True
