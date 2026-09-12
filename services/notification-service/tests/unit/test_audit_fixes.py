"""Regression tests for notification-service audit findings #01 (DSAR strip) and #02 (dedupe atomicity)."""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.email.console import ConsoleEmail
from app.adapters.failover import FailoverEmail, FailoverSms
from app.adapters.renderer.safe import SafeRenderer
from app.adapters.sms.console import ConsoleSms
from app.core.erasure import NotificationErasure
from app.core.models import Template
from app.core.notifier import NotificationService
from app.core.stores import (DeliveryStore, InAppStore, PreferenceStore, TemplateStore)


def run(c):
    return asyncio.run(c)


class _CountingEmail(ConsoleEmail):
    def __init__(self):
        self.sends = 0

    def send(self, to, subject, html, text, sender):
        self.sends += 1
        return True


def _notifier(email=None):
    store = MemoryDataStore()
    svc = NotificationService(
        templates=TemplateStore(store), prefs=PreferenceStore(store),
        deliveries=DeliveryStore(store), inapp=InAppStore(store),
        email=FailoverEmail([email or ConsoleEmail()]), sms=FailoverSms([ConsoleSms()]),
        renderer=SafeRenderer(), email_from="no-reply@x.com", default_locale="en")
    run(TemplateStore(store).upsert(Template("welcome", "en", "Hi $name", "Hello $name")))
    return svc, store


# ── 01: DSAR erasure strips the recipient + payload it actually stores ─────────
def test_erasure_strips_recipient_and_payload():
    svc, store = _notifier()
    run(svc.notify(recipient="victim@x.com", channel="email", template_key="welcome",
                   variables={"name": "Sara"}, tenant="acme"))
    run(NotificationErasure(store=store, pii_salt="p" * 32).erase("acme", "victim@x.com"))
    # every stored delivery for the subject no longer carries the address or the content
    rows, _ = run(store.find("deliveries", {"tenant": "acme"}, limit=100))
    assert rows, "no delivery recorded"
    assert all(r["recipient"] != "victim@x.com" for r in rows)
    assert all(not r.get("payload") for r in rows)


# ── 02: two concurrent same-key sends dispatch once ───────────────────────────
def test_concurrent_idempotent_send_dispatches_once():
    email = _CountingEmail()
    svc, store = _notifier(email=email)

    async def _race():
        return await asyncio.gather(
            svc.notify(recipient="a@b.com", channel="email", template_key="welcome",
                       variables={"name": "X"}, idempotency_key="k1", tenant="t1"),
            svc.notify(recipient="a@b.com", channel="email", template_key="welcome",
                       variables={"name": "X"}, idempotency_key="k1", tenant="t1"))

    run(_race())
    assert email.sends == 1                # atomic reserve prevented the duplicate dispatch
