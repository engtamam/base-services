"""A message queued during an outage must dedupe a caller's retry — or she gets two.

The reported bug: provider down -> delivery QUEUED. The caller's own retry (or a sweep) calls
/notify again with the same idempotency key before the drain runs. A dedup that remembers only
terminal states starts a SECOND queued delivery, and both drain.
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.notifier import QUEUED_MODE, NotificationService
from app.core.retry import QUEUED, RetryPolicy
from app.core.stores import DeliveryStore, InAppStore, PreferenceStore, TemplateStore


def run(c):
    return asyncio.run(c)


class DownProvider:
    def send(self, *a):
        return (False, "prov")


class Renderer:
    def render(self, s, b, v):
        return s, b


def build():
    st = MemoryDataStore()
    svc = NotificationService(
        templates=TemplateStore(st), prefs=PreferenceStore(st),
        deliveries=DeliveryStore(st), inapp=InAppStore(st, page_size=20, scan_limit=100),
        email=DownProvider(), sms=DownProvider(), renderer=Renderer(),
        email_from="x@y", default_locale="en", delivery_mode=QUEUED_MODE,
        retry=RetryPolicy(max_attempts=3, base_ms=1000, max_ms=60000,
                          retryable=("send failed",), permanent=(), unknown_is_retryable=True))
    from app.core.models import Template
    run(TemplateStore(st).upsert(Template(key="reminder", locale="en", subject="S", body="B")))
    return svc, DeliveryStore(st)


def test_a_retry_while_queued_does_not_create_a_second_delivery():
    svc, deliveries = build()
    args = dict(recipient="a@b.com", channel="email", template_key="reminder",
                idempotency_key="rem-42")

    first = run(svc.notify(**args))
    assert first["status"] == QUEUED

    second = run(svc.notify(**args))               # the caller's retry, same key
    assert second.get("duplicate") is True, "the retry must return the existing queued delivery"

    rows = run(deliveries.list(50))
    queued = [r for r in rows if r["status"] == QUEUED]
    assert len(queued) == 1, "exactly ONE queued delivery — a second would drain into a second send"


def test_dedupe_states_are_configurable():
    """If a platform excludes `queued`, it opts back into the old behaviour — explicitly."""
    st = MemoryDataStore()
    from app.core.models import Template
    run(TemplateStore(st).upsert(Template(key="reminder", locale="en", subject="S", body="B")))
    svc = NotificationService(
        templates=TemplateStore(st), prefs=PreferenceStore(st),
        deliveries=DeliveryStore(st), inapp=InAppStore(st, page_size=20, scan_limit=100),
        email=DownProvider(), sms=DownProvider(), renderer=Renderer(),
        email_from="x@y", default_locale="en", delivery_mode=QUEUED_MODE,
        dedupe_states=("sent", "suppressed"),      # queued deliberately excluded
        retry=RetryPolicy(max_attempts=3, base_ms=1000, max_ms=60000,
                          retryable=("send failed",), permanent=(), unknown_is_retryable=True))
    args = dict(recipient="a@b.com", channel="email", template_key="reminder",
                idempotency_key="k")
    run(svc.notify(**args))
    second = run(svc.notify(**args))
    assert second.get("duplicate") is not True, "excluding queued restores the create-a-second path"
