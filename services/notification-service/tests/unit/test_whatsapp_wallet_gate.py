"""The send-time wallet gate — the money-critical invariants, each pinned:

  * gates provider-billed send() only; skips non-billed chains and a disabled gate;
  * within the allowance (or overage 0) → free, no reserve;
  * overage + funds → reserve, then CONFIRM only if the send went out on a billed driver;
  * overage + empty wallet → REFUSE (fail closed, nothing reserved lingers);
  * unreachable wallet/pricing → posture decides (fail_closed refuses, fail_open sends);
  * failover to a free driver → RELEASE, not confirm (a free send is never charged);
  * the counter advances only on a confirmed billed send;
  * send_interactive() is NEVER gated, even with an empty wallet.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.stores import WhatsAppSendCounter
from app.core.whatsapp_wallet_gate import (FAIL_OPEN, STATUS_INSUFFICIENT, STATUS_UNAVAILABLE,
                                           WhatsAppWalletGate)
from app.ports.wallet import INSUFFICIENT, UNAVAILABLE, ReserveResult

NOW = 1_700_000_000.0
PERIOD = "2023-11"


class FakeWallet:
    def __init__(self, reserve_result=ReserveResult(hold_id="H1")):
        self._reserve = reserve_result
        self.calls: list = []

    async def reserve(self, *, tenant, amount, source, reason):
        self.calls.append(("reserve", tenant, amount))
        return self._reserve

    async def confirm(self, *, hold_id):
        self.calls.append(("confirm", hold_id))
        return True

    async def release(self, *, hold_id):
        self.calls.append(("release", hold_id))
        return True


class FakePricing:
    def __init__(self, policy):
        self._policy = policy

    async def get(self, tenant):
        return self._policy


def _gate(*, policy, reserve=ReserveResult(hold_id="H1"), unknown="fail_closed", count_seed=0):
    ds = MemoryDataStore()
    counter = WhatsAppSendCounter(ds)
    if count_seed:
        asyncio.run(ds.put("wa_send_count", f"acme|{PERIOD}",
                           {"id": f"acme|{PERIOD}", "tenant": "acme", "period": PERIOD, "count": count_seed}))
    wallet = FakeWallet(reserve)
    gate = WhatsAppWalletGate(wallet=wallet, pricing=FakePricing(policy), counter=counter,
                              unknown=unknown, clock=lambda: NOW, id_factory=lambda: "rid")
    return gate, wallet, counter


# ── skip conditions ───────────────────────────────────────────────────────────
def test_disabled_gate_skips_every_send():
    gate = WhatsAppWalletGate()                    # no clients wired
    d = asyncio.run(gate.before_send(tenant="acme", drivers=["threesixtydialog"]))
    assert d.action == "skip" and gate.enabled is False


def test_a_non_provider_billed_chain_is_never_gated():
    gate, wallet, _ = _gate(policy={"included_messages_per_period": 0, "overage_halalas_per_message": 5})
    d = asyncio.run(gate.before_send(tenant="acme", drivers=["meta", "console"]))
    assert d.action == "skip" and wallet.calls == []      # meta-direct: the shop's own bill


# ── free tier ──────────────────────────────────────────────────────────────────
def test_within_the_allowance_is_free_without_reserving():
    gate, wallet, _ = _gate(policy={"included_messages_per_period": 10, "overage_halalas_per_message": 5},
                            count_seed=9)
    d = asyncio.run(gate.before_send(tenant="acme", drivers=["threesixtydialog"]))
    assert d.action == "free" and wallet.calls == []


def test_zero_overage_is_free_even_past_the_cap():
    gate, wallet, _ = _gate(policy={"included_messages_per_period": 0, "overage_halalas_per_message": 0},
                            count_seed=100)
    d = asyncio.run(gate.before_send(tenant="acme", drivers=["threesixtydialog"]))
    assert d.action == "free" and wallet.calls == []


# ── overage ──────────────────────────────────────────────────────────────────────
def test_overage_with_funds_reserves():
    gate, wallet, _ = _gate(policy={"included_messages_per_period": 10, "overage_halalas_per_message": 5},
                            count_seed=10)
    d = asyncio.run(gate.before_send(tenant="acme", drivers=["threesixtydialog", "console"]))
    assert d.action == "reserved" and d.hold_id == "H1"
    assert wallet.calls == [("reserve", "acme", 5)]


def test_overage_with_empty_wallet_refuses_fail_closed():
    gate, wallet, _ = _gate(policy={"included_messages_per_period": 0, "overage_halalas_per_message": 5},
                            reserve=ReserveResult(refused=INSUFFICIENT))
    d = asyncio.run(gate.before_send(tenant="acme", drivers=["threesixtydialog"]))
    assert d.action == "refuse" and d.status == STATUS_INSUFFICIENT
    assert d.reason == "insufficient_wallet_balance"


def test_wallet_unreachable_refuses_when_fail_closed():
    gate, _, _ = _gate(policy={"included_messages_per_period": 0, "overage_halalas_per_message": 5},
                       reserve=ReserveResult(refused=UNAVAILABLE), unknown="fail_closed")
    d = asyncio.run(gate.before_send(tenant="acme", drivers=["threesixtydialog"]))
    assert d.action == "refuse" and d.status == STATUS_UNAVAILABLE


def test_wallet_unreachable_sends_when_fail_open():
    gate, _, _ = _gate(policy={"included_messages_per_period": 0, "overage_halalas_per_message": 5},
                       reserve=ReserveResult(refused=UNAVAILABLE), unknown=FAIL_OPEN)
    d = asyncio.run(gate.before_send(tenant="acme", drivers=["threesixtydialog"]))
    assert d.action == "free"


def test_pricing_unreachable_posture():
    closed, _, _ = _gate(policy=None, unknown="fail_closed")
    assert asyncio.run(closed.before_send(tenant="acme", drivers=["threesixtydialog"])).action == "refuse"
    opened, _, _ = _gate(policy=None, unknown=FAIL_OPEN)
    assert asyncio.run(opened.before_send(tenant="acme", drivers=["threesixtydialog"])).action == "free"


# ── settlement (after_send) ──────────────────────────────────────────────────────
def test_a_billed_success_confirms_and_counts():
    gate, wallet, counter = _gate(
        policy={"included_messages_per_period": 0, "overage_halalas_per_message": 5}, count_seed=3)
    d = asyncio.run(gate.before_send(tenant="acme", drivers=["threesixtydialog"]))
    asyncio.run(gate.after_send(d, tenant="acme", sent_ok=True, winning_driver="threesixtydialog"))
    assert ("confirm", "H1") in wallet.calls
    assert asyncio.run(counter.count("acme", now=NOW)) == 4       # advanced by one


def test_failover_to_a_free_driver_releases_and_does_not_count():
    gate, wallet, counter = _gate(
        policy={"included_messages_per_period": 0, "overage_halalas_per_message": 5}, count_seed=3)
    d = asyncio.run(gate.before_send(tenant="acme", drivers=["threesixtydialog", "console"]))
    # 360dialog failed, the send actually went out via console (free) → release, never confirm
    asyncio.run(gate.after_send(d, tenant="acme", sent_ok=True, winning_driver="console"))
    assert ("release", "H1") in wallet.calls and ("confirm", "H1") not in wallet.calls
    assert asyncio.run(counter.count("acme", now=NOW)) == 3       # unchanged


def test_a_failed_send_releases_the_hold():
    gate, wallet, counter = _gate(
        policy={"included_messages_per_period": 0, "overage_halalas_per_message": 5})
    d = asyncio.run(gate.before_send(tenant="acme", drivers=["threesixtydialog"]))
    asyncio.run(gate.after_send(d, tenant="acme", sent_ok=False, winning_driver="threesixtydialog"))
    assert ("release", "H1") in wallet.calls
    assert asyncio.run(counter.count("acme", now=NOW)) == 0


# ── through the notifier: send() gated, send_interactive() NEVER gated ────────────
class _FakeWA:
    driver = "threesixtydialog"

    def send(self, to, template_name, language, variables, biz_opaque=""):
        return True, "threesixtydialog", "wamid.tmpl"

    def send_interactive(self, to, interactive, biz_opaque=""):
        return True, "threesixtydialog", "wamid.int"


def _service_with_refusing_gate():
    from app.adapters.renderer.safe import SafeRenderer
    from app.core.notifier import NotificationService
    from app.core.stores import DeliveryStore, InAppStore, PreferenceStore, TemplateStore
    ds = MemoryDataStore()
    gate = WhatsAppWalletGate(
        wallet=FakeWallet(ReserveResult(refused=INSUFFICIENT)),
        pricing=FakePricing({"included_messages_per_period": 0, "overage_halalas_per_message": 5}),
        counter=WhatsAppSendCounter(ds), unknown="fail_closed")
    svc = NotificationService(
        templates=TemplateStore(ds), prefs=PreferenceStore(ds), deliveries=DeliveryStore(ds),
        inapp=InAppStore(ds), email=None, sms=None, renderer=SafeRenderer(),
        email_from="x@y.test", default_locale="en", whatsapp=_FakeWA(), wa_templates=None,
        whatsapp_wallet_gate=gate)
    return svc


def test_notify_template_send_is_refused_when_the_wallet_is_empty():
    svc = _service_with_refusing_gate()
    res = asyncio.run(svc.notify(recipient="966501234567", channel="whatsapp", template_key="promo",
                                 tenant="acme", variables={"a": "b"}))
    assert res["status"] == STATUS_INSUFFICIENT       # a template send hits the gate and is stopped


def test_interactive_send_is_never_gated_even_with_an_empty_wallet():
    svc = _service_with_refusing_gate()
    res = asyncio.run(svc.send_interactive(recipient="966501234567", tenant="acme",
                                           interactive={"type": "button", "body": {"text": "ok"}},
                                           kind="button"))
    assert res["status"] == "sent"                    # the free in-window path bypasses the gate
