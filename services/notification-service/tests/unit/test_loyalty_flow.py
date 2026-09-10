"""Flow #6 — loyalty redemption: pick → confirm → redeem to SUCCESS, atomic-fail, provider outage."""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.booking_flow import CONFIRM, SELECT, SELECTION_FIELD, SUCCESS, SessionStore
from app.core.loyalty_flow import LoyaltyEngine, ProviderUnavailable


def run(c):
    return asyncio.run(c)


class _Prov:
    def __init__(self, ok=True, reason="", outage=False):
        self.ok, self.reason, self.outage, self.calls = ok, reason, outage, []

    async def balance(self, *, tenant, customer):
        if self.outage:
            raise ProviderUnavailable("down")
        return {"points": 1200, "label": "1,200 pts"}

    async def rewards(self, *, tenant, customer):
        return [{"id": "coffee", "title": "Free coffee — 500 pts"}]

    async def redeem(self, *, tenant, customer, reward, idempotency_key):
        self.calls.append((customer, reward, idempotency_key))
        return {"ok": self.ok, "ref": "VCH-1", "reason": self.reason}


def _eng(p=None):
    return LoyaltyEngine(provider=p or _Prov(), sessions=SessionStore(MemoryDataStore(), clock=lambda: 1000.0))


TOK = "tk"


def _init(e):
    return run(e.handle({"action": "INIT", "flow_token": TOK, "data": {"customer": "c1"}}, tenant="t1"))


def _sub(e, screen, data):
    return run(e.handle({"action": "data_exchange", "screen": screen, "flow_token": TOK, "data": data}, tenant="t1"))


def test_name_defaults_to_loyalty_and_is_configurable():
    # A second instance (e.g. subscription_renewal) reuses the whole engine under its own registry key.
    assert _eng().name == "loyalty"
    renewal = LoyaltyEngine(provider=_Prov(), name="subscription_renewal",
                            sessions=SessionStore(MemoryDataStore(), clock=lambda: 1000.0))
    assert renewal.name == "subscription_renewal"


def test_two_instances_register_under_distinct_names():
    from app.core.flow_registry import FlowRegistry
    reg = FlowRegistry()
    reg.register(_eng())
    reg.register(LoyaltyEngine(provider=_Prov(), name="subscription_renewal",
                               sessions=SessionStore(MemoryDataStore(), clock=lambda: 1000.0)))
    assert set(reg.names()) >= {"loyalty", "subscription_renewal"}
    assert reg.get("subscription_renewal") is not reg.get("loyalty")


def test_redeem_path_to_success():
    p = _Prov()
    e = _eng(p)
    r = _init(e)
    assert r["screen"] == SELECT and "1,200 pts" in r["data"]["title"]
    assert r["data"]["options"][0]["id"] == "coffee"
    r = _sub(e, SELECT, {SELECTION_FIELD: "coffee"})
    assert r["screen"] == CONFIRM
    r = _sub(e, CONFIRM, {})
    assert r["screen"] == SUCCESS
    assert r["data"]["extension_message_response"]["params"]["reward_ref"] == "VCH-1"
    assert p.calls[-1] == ("c1", "coffee", TOK)              # idempotent on the flow_token


def test_a_failed_redeem_stays_on_confirm_with_error():
    e = _eng(_Prov(ok=False, reason="Not enough points"))
    _init(e)
    _sub(e, SELECT, {SELECTION_FIELD: "coffee"})
    r = _sub(e, CONFIRM, {})
    assert r["screen"] == CONFIRM and r["data"]["error_message"] == "Not enough points"


def test_empty_pick_is_refused():
    e = _eng()
    _init(e)
    r = _sub(e, SELECT, {})
    assert r["screen"] == SELECT and "error_message" in r["data"]


def test_provider_outage_is_a_snackbar():
    e = _eng(_Prov(outage=True))
    r = _init(e)
    assert "error_message" in r["data"]                       # never an empty reward list
