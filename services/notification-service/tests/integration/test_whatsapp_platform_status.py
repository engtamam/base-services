"""GET /internal/whatsapp/platform-status — bounded aggregate counts for an operator panel.

Counts only, never row dumps: WhatsApp delivery outcomes, bound-number count, marketing opt-out
count. Every figure is capped (NOTIFY_PLATFORM_STATUS_CAP) so a polled panel never triggers a scan.
"""
from __future__ import annotations

import asyncio
import os

import pytest
from starlette.testclient import TestClient

from app.config import get_settings

ITOKEN = "i" * 40


@pytest.fixture()
def client():
    saved = dict(os.environ)
    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN=ITOKEN,
                      NOTIFY_PLATFORM_STATUS_CAP="3")
    get_settings.cache_clear()
    from app.main import create_app
    with TestClient(create_app()) as c:
        yield c
    os.environ.clear(); os.environ.update(saved)
    get_settings.cache_clear()


def _tok():
    return {"X-Internal-Token": ITOKEN}


def _seed(client):
    from app.core.models import Delivery
    from app.core.stores import DeliveryStore, PreferenceStore, WhatsAppNumberIndex
    ds = client.app.state.container.datastore

    async def seed():
        d = DeliveryStore(ds)
        await d.record(Delivery(id="1", recipient="a", channel="whatsapp", template_key="t",
                                status="sent"))
        await d.record(Delivery(id="2", recipient="b", channel="whatsapp", template_key="t",
                                status="failed"))
        await d.record(Delivery(id="3", recipient="c", channel="whatsapp", template_key="t",
                                status="sent", receipt_status="read", receipt_ts=2.0))
        await d.record(Delivery(id="4", recipient="e", channel="email", template_key="t",
                                status="sent"))          # non-whatsapp: excluded
        await WhatsAppNumberIndex(ds).bind("PN1", "shopA")
        await WhatsAppNumberIndex(ds).bind("PN2", "shopB")
        await PreferenceStore(ds).apply_event_opt_out("966500000001", "marketing", opted_out=True)
    asyncio.run(seed())


def test_platform_status_requires_the_internal_token(client):
    assert client.get("/internal/whatsapp/platform-status").status_code == 401


def test_platform_status_returns_bounded_aggregate_counts(client):
    _seed(client)
    body = client.get("/internal/whatsapp/platform-status", headers=_tok()).json()
    d = body["deliveries"]
    assert d["sent"] == 2 and d["failed"] == 1 and d["read"] == 1   # email excluded
    assert body["bound_numbers"]["count"] == 2
    assert body["marketing_opt_outs"]["count"] == 1
    assert body["cap"] == 3
    # No row-level data leaked — only counts/flags.
    assert set(d) <= {"sent", "failed", "delivered", "read", "sampled", "capped"}


def test_counts_are_capped_and_flagged(client):
    from app.core.stores import WhatsAppNumberIndex
    ds = client.app.state.container.datastore

    async def many():
        for i in range(5):                                # cap is 3
            await WhatsAppNumberIndex(ds).bind(f"PN{i}", f"shop{i}")
    asyncio.run(many())
    body = client.get("/internal/whatsapp/platform-status", headers=_tok()).json()
    assert body["bound_numbers"]["count"] == 3 and body["bound_numbers"]["capped"] is True
