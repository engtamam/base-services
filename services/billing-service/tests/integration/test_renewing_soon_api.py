"""GET /internal/billing/subscriptions/renewing-soon — the read-only pre-renewal horizon read.

Security surface: internal-token only, safe field whitelist (no payment token / no buyer tax
identity), spans tenants, real cursor, and a validated horizon (no unbounded look-ahead).
"""
from __future__ import annotations

import asyncio
import os
import time

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


def _put_sub(client, **over):
    ds = client.app.state.container.datastore
    now = time.time()
    sub = {"id": over["id"], "tenant": over.get("tenant", "t1"), "plan_id": "pro",
           "status": over.get("status", "active"), "interval": over.get("interval", "month"),
           "current_period_end": over.get("current_period_end", now + 3600),
           "limits_snapshot": {"seats": 5}, "created": over.get("created", "2026-01-01T00:00:00+00:00"),
           # fields that must NEVER surface on this read:
           "payment_token": "tok_secret", "buyer_vat": "310000000000003", "buyer_name": "Alice"}
    sub.update({k: v for k, v in over.items()})
    run(ds.put("subscriptions", sub["id"], sub))


def test_requires_the_internal_token(client):
    assert client.get("/internal/billing/subscriptions/renewing-soon").status_code == 401


def test_returns_within_horizon_across_tenants_with_safe_fields_only(client):
    now = time.time()
    _put_sub(client, id="rs_soon", tenant="t1", current_period_end=now + 3600)          # in
    _put_sub(client, id="rs_soon2", tenant="t2", current_period_end=now + 7200)         # in, other tenant
    _put_sub(client, id="rs_far", tenant="t1", current_period_end=now + 30 * 86400)     # out (far)
    _put_sub(client, id="rs_cancelled", tenant="t1", status="canceled",
             current_period_end=now + 3600)                                             # out (status)
    r = client.get("/internal/billing/subscriptions/renewing-soon?within_seconds=172800&limit=100",
                   headers=IT)
    assert r.status_code == 200, r.text
    subs = r.json()["subscriptions"]
    ids = {s["id"] for s in subs}
    assert "rs_soon" in ids and "rs_soon2" in ids                 # spans tenants
    assert "rs_far" not in ids and "rs_cancelled" not in ids
    one = next(s for s in subs if s["id"] == "rs_soon")
    assert set(one) == {"id", "tenant", "plan_id", "status", "current_period_end", "limits_snapshot"}
    # nothing sensitive leaked
    assert "payment_token" not in one and "buyer_vat" not in one and "buyer_name" not in one


def test_horizon_is_validated(client):
    assert client.get("/internal/billing/subscriptions/renewing-soon?within_seconds=0",
                      headers=IT).status_code == 400
    # 91 days > the 90-day cap
    assert client.get("/internal/billing/subscriptions/renewing-soon?within_seconds=7862400",
                      headers=IT).status_code == 400


def test_cursor_paging_is_offered(client):
    now = time.time()
    for i in range(6):
        _put_sub(client, id=f"pg_{i}", tenant="tpage", current_period_end=now + 3600)
    seen, cursor, guard = set(), "", 0
    while True:
        url = f"/internal/billing/subscriptions/renewing-soon?within_seconds=172800&limit=2&cursor={cursor}"
        body = client.get(url, headers=IT).json()
        seen.update(s["id"] for s in body["subscriptions"] if s["id"].startswith("pg_"))
        cursor = body.get("next_cursor") or ""
        guard += 1
        if not cursor or guard > 50:
            break
    assert {f"pg_{i}" for i in range(6)} <= seen        # every seeded page-row surfaced across pages
