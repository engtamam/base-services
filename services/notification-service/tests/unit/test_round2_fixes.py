"""Round-2 fixes (notification-service). Maps to docs/audits/round2/notification-service.json."""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.notifier import NotificationService
from app.core.stores import (DeliveryStore, InAppStore, PreferenceStore, TemplateStore,
                             WhatsAppNumberIndex)
from app.core.whatsapp_optout import apply_user_preferences


def run(c):
    return asyncio.run(c)


def _value(wa_id, decision, phone_number_id):
    return {"messaging_product": "whatsapp",
            "metadata": {"phone_number_id": phone_number_id},
            "user_preferences": [{"wa_id": wa_id, "category": "marketing_messages",
                                  "value": decision, "timestamp": 1}]}


def _svc(ds):
    class _WA:
        driver = "wa"
        def send_template(self, *a, **k):
            return True, "wa", "wamid.X"
    return NotificationService(
        templates=TemplateStore(ds), prefs=PreferenceStore(ds), deliveries=DeliveryStore(ds),
        inapp=InAppStore(ds), email=None, sms=None, renderer=None, email_from="x@y.z",
        default_locale="en", whatsapp=_WA())


# r2-02 — a marketing opt-out via ONE tenant's number does not silence ANOTHER tenant.
def test_optout_is_scoped_per_tenant():
    ds = MemoryDataStore()
    idx = WhatsAppNumberIndex(ds)
    run(idx.bind("PN_A", "tenantA"))          # PN_A routes to tenantA
    prefs = PreferenceStore(ds)
    phone = "966500000001"
    # customer opts out of tenantA's marketing (webhook carries PN_A → resolves to tenantA)
    run(apply_user_preferences(prefs, _value(phone, "stop", "PN_A"), number_index=idx))

    svc = _svc(ds)
    a = run(svc.notify(recipient=phone, channel="whatsapp", template_key="promo",
                       event="marketing", tenant="tenantA"))
    b = run(svc.notify(recipient=phone, channel="whatsapp", template_key="promo",
                       event="marketing", tenant="tenantB"))
    assert a["status"] == "suppressed"        # tenantA honored the opt-out
    assert b["status"] != "suppressed"        # tenantB is NOT silenced by tenantA's opt-out
