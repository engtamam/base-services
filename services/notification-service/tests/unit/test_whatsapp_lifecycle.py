"""WhatsApp template lifecycle + send-time substitution (54.3 + 54.4)."""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.notifier import NotificationService
from app.core.stores import DeliveryStore, InAppStore, PreferenceStore, TemplateStore
from app.core.whatsapp_templates import (APPROVED, PENDING, REJECTED, WhatsAppTemplateRegistry,
                                         WhatsAppTemplateStore)
from app.ports.whatsapp_template_manager import StatusResult, SubmitResult


def run(c):
    return asyncio.run(c)


# ── template lifecycle: submit + sync ─────────────────────────────────────────
class _Manager:
    driver = "fake"

    def __init__(self, submit_status="pending", status="pending", reason=""):
        self._submit = submit_status
        self._status = status
        self._reason = reason

    def submit(self, **kw):
        return SubmitResult(ok=True, provider_template_id="meta:123", status=self._submit)

    def status(self, **kw):
        return StatusResult(status=self._status, reason=self._reason)


def _reg():
    return WhatsAppTemplateRegistry(WhatsAppTemplateStore(MemoryDataStore()))


def test_submit_records_the_provider_id_and_status():
    reg = _reg()
    run(reg.register(tenant="acme", name="otp", language="en", category="AUTHENTICATION"))
    t = run(reg.submit(tenant="acme", name="otp", language="en", manager=_Manager("pending")))
    assert t.provider_template_id == "meta:123" and t.status == PENDING


def test_sync_moves_pending_to_approved():
    reg = _reg()
    run(reg.register(tenant="acme", name="otp", language="en", category="UTILITY"))
    run(reg.submit(tenant="acme", name="otp", language="en", manager=_Manager("pending")))
    out = run(reg.sync(tenant="acme", manager=_Manager(status="approved")))
    assert out["updated"] == 1
    assert run(reg.get("acme", "otp", "en")).status == APPROVED


def test_sync_records_a_rejection_reason():
    reg = _reg()
    run(reg.register(tenant="acme", name="promo", language="en", category="MARKETING"))
    run(reg.submit(tenant="acme", name="promo", language="en", manager=_Manager("pending")))
    run(reg.sync(tenant="acme", manager=_Manager(status="rejected", reason="policy violation")))
    t = run(reg.get("acme", "promo", "en"))
    assert t.status == REJECTED and t.rejection_reason == "policy violation"


# ── send-time substitution ────────────────────────────────────────────────────
class _WA:
    driver = "fake"

    def __init__(self):
        self.calls = []

    def send(self, to, template_name, language, variables):
        self.calls.append((to, template_name, language, list(variables)))
        return True, "fake"


def _svc(reg):
    st = MemoryDataStore()

    class R:
        def render(self, subject, body, variables):
            return subject, body

    wa = _WA()
    svc = NotificationService(
        templates=TemplateStore(st), prefs=PreferenceStore(st), deliveries=DeliveryStore(st),
        inapp=InAppStore(st), email=None, sms=None, whatsapp=wa, wa_templates=reg, renderer=R(),
        email_from="x@y.z", default_locale="en")
    return svc, wa


def test_send_substitutes_variables_in_declared_order():
    reg = _reg()
    t = run(reg.register(tenant="acme", name="otp", language="en", category="AUTHENTICATION",
                         variables=["code", "minutes"]))
    t.status = APPROVED
    run(reg._store.save(t))
    svc, wa = _svc(reg)
    res = run(svc.notify(recipient="+966", channel="whatsapp", template_key="otp", tenant="acme",
                         variables={"code": "123456", "minutes": "5"}))
    assert res["status"] == "sent"
    assert wa.calls[0][3] == ["123456", "5"]        # ordered: code then minutes


def test_send_is_refused_when_not_approved():
    reg = _reg()
    run(reg.register(tenant="acme", name="otp", language="en", category="UTILITY"))  # pending
    svc, wa = _svc(reg)
    res = run(svc.notify(recipient="+966", channel="whatsapp", template_key="otp", tenant="acme",
                         variables={}))
    assert res["status"] == "failed" and wa.calls == []   # not approved -> refused, nothing sent


def test_send_is_refused_on_missing_variables():
    reg = _reg()
    t = run(reg.register(tenant="acme", name="otp", language="en", category="UTILITY",
                         variables=["code"]))
    t.status = APPROVED
    run(reg._store.save(t))
    svc, wa = _svc(reg)
    res = run(svc.notify(recipient="+966", channel="whatsapp", template_key="otp", tenant="acme",
                         variables={}))            # 'code' missing
    assert res["status"] == "failed" and wa.calls == []   # missing var -> refused, nothing sent


def test_send_is_refused_when_template_absent():
    reg = _reg()
    svc, wa = _svc(reg)
    res = run(svc.notify(recipient="+966", channel="whatsapp", template_key="ghost", tenant="acme",
                         variables={}))
    assert res["status"] == "failed"                     # absent template -> refused
