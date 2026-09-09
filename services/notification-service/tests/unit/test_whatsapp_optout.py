"""The WhatsApp marketing opt-out parser: a `user_preferences` webhook durably updates preferences.

stop -> the customer's phone gets "marketing" on its opt-out list; resume -> it comes off. Unknown
categories / malformed values are ignored, never misapplied (a resume mislabelled as a stop is itself
a compliance error).
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.stores import PreferenceStore
from app.core.whatsapp_optout import apply_user_preferences


def run(c):
    return asyncio.run(c)


def _value(wa_id: str, decision: str, category: str = "marketing_messages"):
    return {"messaging_product": "whatsapp",
            "metadata": {"phone_number_id": "PN1"},
            "user_preferences": [{"wa_id": wa_id, "detail": "d", "category": category,
                                  "value": decision, "timestamp": 1731705721}]}


def test_stop_opts_the_phone_out_of_marketing():
    prefs = PreferenceStore(MemoryDataStore())
    n = run(apply_user_preferences(prefs, _value("966500000001", "stop")))
    assert n == 1
    pref = run(prefs.get("966500000001"))
    assert "marketing" in pref.opt_out_events
    assert pref.allows("whatsapp", "marketing") is False
    assert pref.allows("whatsapp", "otp") is True          # only marketing is blocked


def test_resume_opts_back_in():
    prefs = PreferenceStore(MemoryDataStore())
    run(apply_user_preferences(prefs, _value("966500000001", "stop")))
    run(apply_user_preferences(prefs, _value("966500000001", "resume")))
    pref = run(prefs.get("966500000001"))
    assert "marketing" not in pref.opt_out_events
    assert pref.allows("whatsapp", "marketing") is True


def test_unknown_category_or_value_is_ignored():
    prefs = PreferenceStore(MemoryDataStore())
    assert run(apply_user_preferences(prefs, _value("p", "stop", category="promos"))) == 0
    assert run(apply_user_preferences(prefs, _value("p", "maybe"))) == 0
    assert run(prefs.get("p")).opt_out_events == []
