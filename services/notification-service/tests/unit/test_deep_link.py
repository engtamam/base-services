"""In-app notification deep link (Raje3 wiring): an optional internal path the notification bell
navigates to on tap. Additive + backward-compatible — an item with no link behaves exactly as before."""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.email.console import ConsoleEmail
from app.adapters.failover import FailoverEmail, FailoverSms
from app.adapters.renderer.safe import SafeRenderer
from app.adapters.sms.console import ConsoleSms
from app.core.models import InAppItem, Template
from app.core.notifier import NotificationService
from app.core.stores import DeliveryStore, InAppStore, PreferenceStore, TemplateStore, _INAPP


def run(c):
    return asyncio.run(c)


def _notifier():
    store = MemoryDataStore()
    svc = NotificationService(
        templates=TemplateStore(store), prefs=PreferenceStore(store),
        deliveries=DeliveryStore(store), inapp=InAppStore(store),
        email=FailoverEmail([ConsoleEmail()]), sms=FailoverSms([ConsoleSms()]),
        renderer=SafeRenderer(), email_from="no-reply@x.com", default_locale="en")
    run(TemplateStore(store).upsert(Template("order_new", "en", "New order", "Order $n is in")))
    return svc, store


# ── with a deep link: it reaches the feed ─────────────────────────────────────
def test_inapp_notification_carries_the_deep_link_to_the_feed():
    svc, store = _notifier()
    res = run(svc.notify(recipient="u1", channel="inapp", template_key="order_new",
                         user_id="u1", variables={"n": "102"}, deep_link="/whatsapp-orders/102"))
    assert res["status"] == "sent"
    feed = run(InAppStore(store).feed("u1"))
    assert len(feed) == 1 and feed[0]["deep_link"] == "/whatsapp-orders/102"


# ── without a deep link: unchanged behaviour, stable shape ────────────────────
def test_inapp_notification_without_a_deep_link_defaults_to_empty():
    svc, store = _notifier()
    run(svc.notify(recipient="u1", channel="inapp", template_key="order_new",
                   user_id="u1", variables={"n": "1"}))
    feed = run(InAppStore(store).feed("u1"))
    assert feed[0]["deep_link"] == ""           # present and empty — the bell just marks-as-read


# ── backward-compat: a pre-existing item written WITHOUT the field still reads ──
def test_legacy_item_missing_the_field_is_normalised_on_read():
    store = MemoryDataStore()
    # simulate a row persisted before deep_link existed (no such key)
    run(store.put(_INAPP, "old1", {"id": "old1", "user": "u1", "title": "t", "body": "b",
                                   "read": False, "ts": "2020-01-01T00:00:00Z"}))
    feed = run(InAppStore(store).feed("u1"))
    assert feed[0]["id"] == "old1" and feed[0]["deep_link"] == ""   # normalised, not a KeyError


# ── other channels ignore the deep link (no crash, no leakage) ────────────────
def test_email_channel_ignores_deep_link():
    svc, _ = _notifier()
    res = run(svc.notify(recipient="a@b.com", channel="email", template_key="order_new",
                         variables={"n": "5"}, deep_link="/whatsapp-orders/5"))
    assert res["status"] == "sent"              # email send unaffected by the in-app-only field


# ── the model round-trips the field ───────────────────────────────────────────
def test_model_to_dict_includes_deep_link():
    d = InAppItem(id="i", user="u", title="t", body="b", deep_link="/x/1").to_dict()
    assert d["deep_link"] == "/x/1"
    assert InAppItem(id="i", user="u", title="t", body="b").to_dict()["deep_link"] == ""


# ── API edge: deep_link must be a RELATIVE internal path (no external redirect) ─
@pytest.mark.parametrize("bad", ["http://evil.example/x", "//evil.example", "javascript:alert(1)",
                                 "/with space", "relative/no/slash"])
def test_notifyin_rejects_non_relative_deep_links(bad):
    from pydantic import ValidationError
    from app.api.internal import NotifyIn
    with pytest.raises(ValidationError):
        NotifyIn(recipient="u1", channel="inapp", template_key="order_new", deep_link=bad)


@pytest.mark.parametrize("ok", ["/whatsapp-orders/102", "/orders", "/a/b/c?x=1"])
def test_notifyin_accepts_relative_deep_links(ok):
    from app.api.internal import NotifyIn
    assert NotifyIn(recipient="u1", channel="inapp", template_key="order_new",
                    deep_link=ok).deep_link == ok
