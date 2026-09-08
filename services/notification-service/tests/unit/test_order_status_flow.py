"""Flow #3 — order status (read-only): INIT shows live status; Done → SUCCESS; outage → snackbar."""
from __future__ import annotations

import asyncio

from app.core.booking_flow import SUCCESS, ProviderUnavailable
from app.core.order_status_flow import STATUS, OrderStatusEngine


def run(c):
    return asyncio.run(c)


class _Prov:
    def __init__(self, outage=False):
        self.outage, self.seen = outage, []

    async def status(self, *, tenant, order):
        self.seen.append(order)
        if self.outage:
            raise ProviderUnavailable("down")
        return {"headline": "Out for delivery", "body": "Tracking 123 — ETA 20 min"}


def test_init_shows_live_status():
    p = _Prov()
    e = OrderStatusEngine(provider=p)
    r = run(e.handle({"action": "INIT", "flow_token": "tk", "data": {"order": "ord9"}}, tenant="t1"))
    assert r["screen"] == STATUS and r["data"]["headline"] == "Out for delivery"
    assert "Tracking 123" in r["data"]["body"] and p.seen == ["ord9"]


def test_done_completes_the_flow():
    e = OrderStatusEngine(provider=_Prov())
    r = run(e.handle({"action": "data_exchange", "screen": STATUS, "flow_token": "tk", "data": {}}, tenant="t1"))
    assert r["screen"] == SUCCESS
    assert r["data"]["extension_message_response"]["params"]["flow_token"] == "tk"


def test_outage_shows_a_message_not_a_crash():
    e = OrderStatusEngine(provider=_Prov(outage=True))
    r = run(e.handle({"action": "INIT", "flow_token": "tk", "data": {"order": "x"}}, tenant="t1"))
    assert r["screen"] == STATUS and "error_message" in r["data"]
