"""End to end: a signed user_preferences webhook durably suppresses later marketing sends.

This is the compliance gap the peer flagged — the opt-out event was published to the at-most-once bus
and consumed by nobody, so Preferences never changed and marketing kept flowing to someone who
unsubscribed (a WhatsApp-account-suspension risk). Now the webhook writes the opt-out durably and
the send path honors it.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os

import pytest
from starlette.testclient import TestClient

from app.config import get_settings
from app.core.notifier import NotificationService
from app.core.stores import DeliveryStore, InAppStore, PreferenceStore, TemplateStore

SECRET = "app-secret-xyz"


@pytest.fixture()
def client():
    saved = dict(os.environ)
    os.environ.update(ENV="test", META_APP_SECRET=SECRET, INTERNAL_SERVICE_TOKEN="i" * 40,
                      TOKEN_VERIFIER_DRIVER="hs256", TOKEN_HS256_SECRET="h" * 40)
    get_settings.cache_clear()
    from app.main import create_app
    with TestClient(create_app()) as c:
        yield c
    os.environ.clear(); os.environ.update(saved)
    get_settings.cache_clear()


def _sign(raw: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()


def _pref_webhook(wa_id: str, decision: str):
    return {"object": "whatsapp_business_account", "entry": [{"id": "WABA1", "changes": [{
        "field": "user_preferences", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "+966", "phone_number_id": "PN1"},
            "user_preferences": [{"wa_id": wa_id, "detail": "Stop", "category": "marketing_messages",
                                  "value": decision, "timestamp": 1731705721}]}}]}]}


def _post(client, payload):
    raw = json.dumps(payload).encode()
    return client.post("/webhooks/whatsapp", content=raw,
                       headers={"X-Hub-Signature-256": _sign(raw)})


def test_optout_webhook_durably_updates_preferences(client):
    phone = "966500000009"
    r = _post(client, _pref_webhook(phone, "stop"))
    assert r.status_code == 200 and r.json()["received"] is True
    ds = client.app.state.container.datastore
    # PN1 is not bound to a tenant → the opt-out is stored under the global (tenant="") scoped key
    # (notification-r2-02), not the raw phone.
    from app.core.whatsapp_optout import wa_pref_key
    pref = asyncio.run(PreferenceStore(ds).get(wa_pref_key("", phone)))
    assert "marketing" in pref.opt_out_events            # persisted, not just published


def test_marketing_send_to_an_opted_out_phone_is_suppressed():
    """The send path (no HTTP) honors the durable opt-out: marketing suppressed, OTP still delivered."""
    ds = _MemStore()

    async def scenario():
        prefs = PreferenceStore(ds)
        await prefs.apply_event_opt_out("966500000009", "marketing", opted_out=True)
        svc = _svc(ds)
        blocked = await svc.notify(recipient="966500000009", channel="whatsapp",
                                   template_key="promo", event="marketing", tenant="t1")
        # A transactional message to the same number is unaffected.
        return blocked

    blocked = asyncio.run(scenario())
    assert blocked["status"] == "suppressed"


# ── minimal service wiring for the send-path test ────────────────────────────
from app.adapters.datastore.memory import MemoryDataStore as _MemStore  # noqa: E402


def _svc(ds):
    class _WA:
        driver = "wa"

        def send_template(self, *a, **k):
            return True, "wa", "wamid.X"

    return NotificationService(
        templates=TemplateStore(ds), prefs=PreferenceStore(ds), deliveries=DeliveryStore(ds),
        inapp=InAppStore(ds), email=None, sms=None, renderer=None, email_from="x@y.z",
        default_locale="en", whatsapp=_WA())
