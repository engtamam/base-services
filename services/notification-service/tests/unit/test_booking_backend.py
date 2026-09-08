"""HTTP booking backend (hermetic, injected transport) + config definition parsing."""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.booking.config_defs import ConfigDefinitionProvider, parse_definition
from app.adapters.booking.http import HttpBookingBackend
from app.adapters.booking.null import NullBookingBackend
from app.core.booking_flow import FlowDefinition, ProviderUnavailable


def run(c):
    return asyncio.run(c)


def _backend(responses):
    """responses: dict op -> (status, body). transport records calls."""
    seen = []

    async def transport(url, payload, headers):
        seen.append(payload)
        return responses[payload["op"]]
    return HttpBookingBackend("https://shop/booking", "tok", transport=transport), seen


def test_http_backend_maps_each_op():
    b, seen = _backend({
        "options": (200, {"options": [{"id": "cut", "title": "Haircut"}]}),
        "calendar": (200, {"min_date": "2026-09-08", "unavailable_dates": ["2026-09-10"]}),
        "slots": (200, {"slots": [{"id": "s1", "title": "10:00"}]}),
        "reserve": (200, {"ok": True, "ref": "BK1"})})
    assert run(b.options(tenant="t", dimension="service", chosen={}, customer="c1"))[0]["id"] == "cut"
    assert run(b.calendar(tenant="t", chosen={}))["unavailable_dates"] == ["2026-09-10"]
    assert run(b.slots(tenant="t", chosen={}, date="2026-09-09"))[0]["title"] == "10:00"
    assert run(b.reserve(tenant="t", selection={}, idempotency_key="k", customer="c1"))["ref"] == "BK1"
    # the op + idempotency key + customer reached the platform
    assert seen[-1] == {"op": "reserve", "tenant": "t", "selection": {}, "idempotency_key": "k",
                        "customer": "c1"}
    assert seen[0] == {"op": "options", "tenant": "t", "dimension": "service", "chosen": {},
                       "customer": "c1"}


def test_a_non_2xx_is_provider_unavailable_not_empty():
    b, _ = _backend({"calendar": (503, {})})
    with pytest.raises(ProviderUnavailable):
        run(b.calendar(tenant="t", chosen={}))


def test_null_backend_refuses_cleanly():
    n = NullBookingBackend()
    assert run(n.options(tenant="t", dimension="service", chosen={})) == []
    assert run(n.calendar(tenant="t", chosen={})) == {}
    r = run(n.reserve(tenant="t", selection={}, idempotency_key="k"))
    assert r["ok"] is False and "reason" in r


# ── config definition parsing ────────────────────────────────────────────────
def test_parse_valid_definition():
    raw = '{"steps":[{"dimension":"service"},{"dimension":"date"},{"dimension":"time"},{"dimension":"confirm"}],"mode":"single","strings":{"pick_service":"اختر الخدمة"}}'
    d = parse_definition(raw)
    assert isinstance(d, FlowDefinition) and d.label("pick_service", "x") == "اختر الخدمة"


def test_parse_empty_or_invalid_is_none():
    assert parse_definition("") is None
    assert parse_definition("not json") is None
    assert parse_definition('{"steps":[]}') is None            # invalid: no date/confirm
    assert run(ConfigDefinitionProvider("").get("t")) is None
