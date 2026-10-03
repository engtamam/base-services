"""Non-security MEDIUM remediation (notification-service):
  008  same-second receipts resolve by status RANK, not arrival order (read is not clobbered by delivered).
  010  the operator delivery log is an ordered, indexed read (newest first), not an unordered scan.
  012  an unparseable accepted WhatsApp send yields a UNIQUE wamid sentinel (no cross-send collision).
  014  raw-mode WhatsApp variables order NUMERICALLY ({{2}} before {{10}}).
  019  a malformed internal-caller key map is refused at boot, not swallowed to {}.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.config import Settings
from app.core.models import Delivery
from app.core.notifier import _var_order
from app.core.stores import DeliveryStore


def run(c):
    return asyncio.run(c)


def _delivery(did, wamid, status="sent"):
    return Delivery(id=did, recipient="+1", channel="whatsapp", template_key="t",
                    status=status, provider_message_id=wamid)


# ── 008: same-second receipt rank ─────────────────────────────────────────────
def test_same_second_read_is_not_clobbered_by_delivered():
    st = MemoryDataStore()
    store = DeliveryStore(st)
    run(store.record(_delivery("d1", "wamid-1")))
    # read arrives first at ts=100
    run(store.apply_receipt("wamid-1", "read", 100.0))
    # a same-second 'delivered' must NOT overwrite the higher-ranked 'read'
    run(store.apply_receipt("wamid-1", "delivered", 100.0))
    d = run(store.by_provider_message_id("wamid-1"))
    assert d.receipt_status == "read"


def test_later_timestamp_advances_normally():
    st = MemoryDataStore()
    store = DeliveryStore(st)
    run(store.record(_delivery("d1", "wamid-1")))
    run(store.apply_receipt("wamid-1", "delivered", 100.0))
    run(store.apply_receipt("wamid-1", "read", 101.0))
    assert run(store.by_provider_message_id("wamid-1")).receipt_status == "read"


# ── 010: operator log is ordered, newest first ────────────────────────────────
def test_operator_log_returns_recent_first():
    st = MemoryDataStore()
    store = DeliveryStore(st)
    for i, ts in enumerate((10.0, 30.0, 20.0)):
        d = _delivery(f"d{i}", f"w{i}")
        d.ts = ts
        run(store.record(d))
    rows = run(store.list(10))
    tss = [r["ts"] for r in rows]
    assert tss == sorted(tss, reverse=True)       # newest first, not insertion order


# ── 012: unique unparsed sentinel ─────────────────────────────────────────────
def test_unparsed_wamid_sentinel_is_unique(monkeypatch):
    from app.adapters.whatsapp import meta as meta_mod

    m = meta_mod.MetaWhatsApp.__new__(meta_mod.MetaWhatsApp)
    m._base = "https://graph.example"
    m._phone_id = "pid"
    m._headers = {}
    monkeypatch.setattr(meta_mod, "post_json", lambda *a, **k: (200, "not-json"))
    a = m._post({})
    b = m._post({})
    assert a.startswith("wamid:unparsed:") and b.startswith("wamid:unparsed:")
    assert a != b                                 # no cross-send collision


# ── 014: numeric raw-mode ordering ────────────────────────────────────────────
def test_var_order_sorts_numeric_keys_numerically():
    variables = {"1": "a", "2": "b", "10": "c", "name": "z"}
    ordered = [v for _, v in sorted(variables.items(), key=_var_order)]
    assert ordered == ["a", "b", "c", "z"]        # {{10}} after {{2}}, named last


# ── 019: malformed caller map refused at boot ─────────────────────────────────
def _settings(**over):
    base = dict(_env_file=None, INTERNAL_SERVICE_TOKEN="x" * 40)
    base.update(over)
    return Settings(**base)


def test_malformed_static_caller_map_refused():
    s = _settings(INTERNAL_CALLER_DRIVER="static", INTERNAL_CALLER_PUBLIC_KEYS_JSON="{not json")
    with pytest.raises(ValueError):
        s.assert_internal_caller_config_ok()


def test_empty_static_caller_map_refused():
    s = _settings(INTERNAL_CALLER_DRIVER="static", INTERNAL_CALLER_PUBLIC_KEYS_JSON="")
    with pytest.raises(ValueError):
        s.assert_internal_caller_config_ok()


def test_valid_static_caller_map_accepted():
    s = _settings(INTERNAL_CALLER_DRIVER="static",
                  INTERNAL_CALLER_PUBLIC_KEYS_JSON='{"billing-service": "PEMDATA"}')
    s.assert_internal_caller_config_ok()          # no raise


def test_shared_driver_ignores_the_maps():
    _settings(INTERNAL_CALLER_DRIVER="shared").assert_internal_caller_config_ok()
