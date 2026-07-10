"""Unit tests: safe renderer, failover, providers' request shape, notifier flows."""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.email.console import ConsoleEmail
from app.adapters.failover import FailoverEmail, FailoverSms
from app.adapters.renderer.safe import SafeRenderer
from app.adapters.sms.console import ConsoleSms
from app.core.notifier import NotificationService
from app.core.models import Preference, Template
from app.core.stores import (
    DeliveryStore, InAppStore, PreferenceStore, TemplateStore,
)
from app.ports.errors import PortError


def run(c):
    return asyncio.run(c)


# ── Safe renderer (no SSTI, CWE-94/1336) ─────────────────────────────────────
def test_renderer_substitutes():
    s, b = SafeRenderer().render("Hi $name", "Code: $code", {"name": "Sara", "code": "123"})
    assert s == "Hi Sara" and b == "Code: 123"


def test_renderer_injection_is_inert():
    # A value that looks like an expression is inserted literally, never evaluated.
    s, b = SafeRenderer().render("x", "$v", {"v": "${7*7}"})
    assert b == "${7*7}"  # not "49"


def test_renderer_missing_var_errors():
    with pytest.raises(PortError):
        SafeRenderer().render("x", "Hi $name", {})


# ── Failover ─────────────────────────────────────────────────────────────────
class _FailEmail:
    driver = "fail"

    def send(self, *a):
        return False


def test_failover_falls_through_to_console():
    fo = FailoverEmail([_FailEmail(), ConsoleEmail()])
    ok, provider = fo.send("a@b.com", "s", "h", "t", "f@b.com")
    assert ok is True and provider == "console"


def test_failover_all_fail():
    ok, provider = FailoverEmail([_FailEmail()]).send("a@b.com", "s", "h", "t", "f@b.com")
    assert ok is False


# ── Provider request shape (no network — inspect the built request) ──────────
def test_resend_builds_bearer_and_payload(monkeypatch):
    import app.adapters.email.resend as r
    captured = {}

    def fake_post_json(url, payload, headers=None):
        captured.update(url=url, payload=payload, headers=headers)
        return 200, "{}"

    monkeypatch.setattr(r, "post_json", fake_post_json)
    assert r.ResendEmail("re_key").send("to@x.com", "Sub", "<b>h</b>", "t", "from@x.com") is True
    assert captured["url"] == "https://api.resend.com/emails"
    assert captured["headers"]["Authorization"] == "Bearer re_key"
    assert captured["payload"]["to"] == ["to@x.com"]


def test_unifonic_builds_ksa_form(monkeypatch):
    import app.adapters.sms.unifonic as u
    captured = {}

    def fake_post_form(url, fields, headers=None):
        captured.update(url=url, fields=fields)
        return 200, "ok"

    monkeypatch.setattr(u, "post_form", fake_post_form)
    assert u.UnifonicSms("app123", "MyBrand").send("+966500000000", "hi") is True
    assert captured["url"] == "https://el.cloud.unifonic.com/rest/SMS/messages"
    assert captured["fields"]["AppSid"] == "app123"
    assert captured["fields"]["Recipient"] == "+966500000000"


# ── NotificationService flows ────────────────────────────────────────────────
def _notifier():
    store = MemoryDataStore()
    return NotificationService(
        templates=TemplateStore(store), prefs=PreferenceStore(store),
        deliveries=DeliveryStore(store), inapp=InAppStore(store),
        email=FailoverEmail([ConsoleEmail()]), sms=FailoverSms([ConsoleSms()]),
        renderer=SafeRenderer(), email_from="no-reply@x.com", default_locale="en",
    ), store


def test_notify_email_records_delivery():
    svc, store = _notifier()
    run(TemplateStore(store).upsert(Template("welcome", "en", "Hi $name", "Hello $name")))
    res = run(svc.notify(recipient="a@b.com", channel="email", template_key="welcome",
                         variables={"name": "Sara"}))
    assert res["status"] == "sent" and res["provider"] == "console"


def test_locale_fallback():
    svc, store = _notifier()
    run(TemplateStore(store).upsert(Template("welcome", "en", "S", "Hello $name")))
    res = run(svc.notify(recipient="a@b.com", channel="email", template_key="welcome",
                         locale="ar", variables={"name": "X"}))  # ar missing -> en
    assert res["status"] == "sent"


def test_missing_template_fails():
    svc, _ = _notifier()
    res = run(svc.notify(recipient="a@b.com", channel="email", template_key="nope"))
    assert res["status"] == "failed"


def test_preference_suppression():
    svc, store = _notifier()
    run(TemplateStore(store).upsert(Template("promo", "en", "S", "B")))
    run(PreferenceStore(store).set(Preference(user="u1", suppressed=True)))
    res = run(svc.notify(recipient="a@b.com", channel="email", template_key="promo", user_id="u1"))
    assert res["status"] == "suppressed"


def test_inapp_feed_and_read():
    svc, store = _notifier()
    run(TemplateStore(store).upsert(Template("ping", "en", "Title", "Body $x")))
    run(svc.notify(recipient="u1", channel="inapp", template_key="ping",
                   variables={"x": "1"}, user_id="u1"))
    feed = run(svc.feed("u1"))
    assert len(feed) == 1 and feed[0]["read"] is False
    assert run(svc.mark_read("u1", feed[0]["id"])) is True
    assert run(svc.feed("u1"))[0]["read"] is True
