"""Tenant-scoped WhatsApp deliveries: a send stamps the Delivery with its tenant, the delivery
store reads one shop's rows by tenant+channel, and the phone_number_id→tenant index round-trips —
the pieces behind the operator's 'why is this shop's WhatsApp broken' diagnostic."""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.notifier import NotificationService
from app.core.models import Preference
from app.core.stores import (DeliveryStore, InAppStore, PreferenceStore, TemplateStore,
                             WhatsAppNumberIndex)
from app.core.whatsapp_templates import APPROVED, WhatsAppTemplateRegistry, WhatsAppTemplateStore


def run(c):
    return asyncio.run(c)


class _WA:
    driver = "fake"

    def send(self, to, template_name, language, variables, biz_opaque_callback_data="",
             flow_token="", flow_action_data=None, header_media_id="", button_payload=""):
        return True, "fake", "wamid.fake"


def _svc(st, reg):
    class R:
        def render(self, subject, body, variables):
            return subject, body

    return NotificationService(
        templates=TemplateStore(st), prefs=PreferenceStore(st), deliveries=DeliveryStore(st),
        inapp=InAppStore(st), email=None, sms=None, whatsapp=_WA(), wa_templates=reg, renderer=R(),
        email_from="x@y.z", default_locale="en")


def _approved_reg(st, tenant="acme", name="otp"):
    reg = WhatsAppTemplateRegistry(WhatsAppTemplateStore(st))
    t = run(reg.register(tenant=tenant, name=name, language="en", category="AUTHENTICATION",
                         variables=["code"]))
    t.status = APPROVED
    run(reg._store.save(t))
    return reg


class _WACapture:
    driver = "fake"

    def __init__(self):
        self.header_media_id = "UNSET"
        self.button_payload = "UNSET"

    def send(self, to, template_name, language, variables, biz_opaque_callback_data="",
             flow_token="", flow_action_data=None, header_media_id="", button_payload=""):
        self.header_media_id = header_media_id
        self.button_payload = button_payload
        return True, "fake", "wamid.fake"


def test_header_media_id_threads_from_notify_to_the_provider():
    st = MemoryDataStore()
    wa = _WACapture()
    reg = _approved_reg(st)

    class R:
        def render(self, subject, body, variables):
            return subject, body

    svc = NotificationService(
        templates=TemplateStore(st), prefs=PreferenceStore(st), deliveries=DeliveryStore(st),
        inapp=InAppStore(st), email=None, sms=None, whatsapp=wa, wa_templates=reg, renderer=R(),
        email_from="x@y.z", default_locale="en")
    res = run(svc.notify(recipient="+966500000009", channel="whatsapp", template_key="otp",
                         tenant="acme", variables={"code": "123456"}, header_media_id="MEDIA-42"))
    assert res["status"] == "sent"
    assert wa.header_media_id == "MEDIA-42"


def test_header_media_id_defaults_to_empty_when_absent():
    st = MemoryDataStore()
    wa = _WACapture()
    reg = _approved_reg(st)

    class R:
        def render(self, subject, body, variables):
            return subject, body

    svc = NotificationService(
        templates=TemplateStore(st), prefs=PreferenceStore(st), deliveries=DeliveryStore(st),
        inapp=InAppStore(st), email=None, sms=None, whatsapp=wa, wa_templates=reg, renderer=R(),
        email_from="x@y.z", default_locale="en")
    run(svc.notify(recipient="+966500000010", channel="whatsapp", template_key="otp",
                   tenant="acme", variables={"code": "1"}))
    assert wa.header_media_id == ""
    assert wa.button_payload == ""


def test_button_payload_threads_from_notify_to_the_provider():
    st = MemoryDataStore()
    wa = _WACapture()
    reg = _approved_reg(st)

    class R:
        def render(self, subject, body, variables):
            return subject, body

    svc = NotificationService(
        templates=TemplateStore(st), prefs=PreferenceStore(st), deliveries=DeliveryStore(st),
        inapp=InAppStore(st), email=None, sms=None, whatsapp=wa, wa_templates=reg, renderer=R(),
        email_from="x@y.z", default_locale="en")
    res = run(svc.notify(recipient="+966500000011", channel="whatsapp", template_key="otp",
                         tenant="acme", variables={"code": "123456"}, button_payload="book:svc_7"))
    assert res["status"] == "sent"
    assert wa.button_payload == "book:svc_7"


def test_whatsapp_send_stamps_the_delivery_with_its_tenant():
    st = MemoryDataStore()
    svc = _svc(st, _approved_reg(st))
    res = run(svc.notify(recipient="+966500000001", channel="whatsapp", template_key="otp",
                         tenant="acme", variables={"code": "123456"}))
    assert res["status"] == "sent"
    rows = run(DeliveryStore(st).by_tenant("acme", channel="whatsapp"))
    assert len(rows) == 1 and rows[0]["tenant"] == "acme"
    assert rows[0]["provider_message_id"] == "wamid.fake"


def test_by_tenant_is_scoped_and_never_cross_tenant():
    st = MemoryDataStore()
    svc = _svc(st, _approved_reg(st, tenant="acme"))
    reg_b = _approved_reg(st, tenant="beta")
    run(svc.notify(recipient="+9661", channel="whatsapp", template_key="otp", tenant="acme",
                   variables={"code": "1"}))
    # a second shop's send must not appear in the first shop's view
    svc_b = _svc(st, reg_b)
    run(svc_b.notify(recipient="+9662", channel="whatsapp", template_key="otp", tenant="beta",
                     variables={"code": "2"}))
    assert {r["tenant"] for r in run(DeliveryStore(st).by_tenant("acme"))} == {"acme"}
    assert run(DeliveryStore(st).by_tenant("")) == []      # no tenant → nothing, never a dump


def test_a_suppressed_whatsapp_send_still_carries_the_tenant():
    # The diagnostic must see the "why it did NOT send" cases too — a suppression is recorded by
    # notify() itself, not the whatsapp path, so it needs the tenant just the same.
    st = MemoryDataStore()
    prefs = PreferenceStore(st)
    run(prefs.set(Preference(user="u1", opt_out_channels=["whatsapp"])))
    svc = _svc(st, _approved_reg(st))
    res = run(svc.notify(recipient="+966", channel="whatsapp", template_key="otp", tenant="acme",
                         user_id="u1", variables={"code": "1"}))
    assert res["status"] == "suppressed"
    rows = run(DeliveryStore(st).by_tenant("acme", channel="whatsapp"))
    assert rows and rows[0]["status"] == "suppressed" and rows[0]["tenant"] == "acme"


def test_number_index_round_trips():
    st = MemoryDataStore()
    idx = WhatsAppNumberIndex(st)
    run(idx.bind("109998887776", "acme"))
    assert run(idx.tenant_of("109998887776")) == "acme"
    assert run(idx.tenant_of("nope")) is None
    run(idx.bind("", "acme"))                              # empty inputs are no-ops, never a bad row
    assert run(idx.tenant_of("")) is None
