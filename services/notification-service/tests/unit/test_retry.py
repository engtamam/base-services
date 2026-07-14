"""An outage must DELAY a message, not destroy it.

The send was inline. Provider down -> recorded "failed" -> gone. A five-minute outage silently
destroyed every reminder sent during it, and the only trace was a row nothing would ever read
again. The message was not delayed. It was lost, and nobody was told.
"""
from __future__ import annotations

import asyncio
import random

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.notifier import INLINE, QUEUED_MODE, NotificationService
from app.core.retry import (
    DEAD_LETTER,
    FAILED,
    QUEUED,
    SENT,
    RetryPolicy,
)
from app.core.stores import DeliveryStore, InAppStore, PreferenceStore, TemplateStore


def run(c):
    return asyncio.run(c)


class Provider:
    """A provider that can be down, flaky, or fussy about the address."""

    def __init__(self, ok=True, error=None):
        self.ok = ok
        self.error = error
        self.calls = 0

    def send(self, *args):
        self.calls += 1
        if self.error:
            raise self.error
        return (True, "prov") if self.ok else (False, "prov")


class Renderer:
    driver = "r"

    def render(self, subject, body, variables):
        return subject, body


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def policy(**kw):
    base = dict(max_attempts=3, base_ms=1000, max_ms=60_000,
                retryable=("timeout", "unavailable", "send failed"),
                permanent=("invalid", "not found"),
                unknown_is_retryable=True)
    base.update(kw)
    return RetryPolicy(**base)


def build(email, *, mode=QUEUED_MODE, clock=None, retry=None):
    st = MemoryDataStore()
    clock = clock or Clock()
    svc = NotificationService(
        templates=TemplateStore(st), prefs=PreferenceStore(st),
        deliveries=DeliveryStore(st), inapp=InAppStore(st, page_size=20, scan_limit=100),
        email=email, sms=Provider(), renderer=Renderer(),
        email_from="no-reply@x", default_locale="en",
        delivery_mode=mode, retry=retry or policy(), clock=clock)
    return svc, DeliveryStore(st), TemplateStore(st), clock


def a_template(templates, key="reminder"):
    from app.core.models import Template
    run(templates.upsert(Template(key=key, locale="en", subject="S", body="B")))


def notify(svc, **kw):
    args = dict(recipient="a@b.com", channel="email", template_key="reminder")
    args.update(kw)
    return run(svc.notify(**args))


# ── the outage ───────────────────────────────────────────────────────────────
def test_a_provider_outage_QUEUES_the_message_instead_of_losing_it():
    """THE fix."""
    email = Provider(ok=False)
    svc, deliveries, templates, clock = build(email)
    a_template(templates)

    res = notify(svc)
    assert res["status"] == QUEUED, "a message lost to an outage is the bug this exists to remove"
    assert res["retry_at"] > clock.t, "and it must say WHEN it will try again"
    assert res["attempts"] == 1


def test_the_drain_worker_sends_it_when_the_provider_comes_back():
    email = Provider(ok=False)
    svc, deliveries, templates, clock = build(email)
    a_template(templates)
    notify(svc)

    clock.t += 3600                       # the backoff has long expired
    email.ok = True                       # the provider is back
    out = run(svc.drain())

    assert out["sent"] == 1
    rows = run(deliveries.list(10))
    assert rows[0]["status"] == SENT
    assert rows[0]["attempts"] == 2, "the retry is the second attempt, and we counted it"


def test_a_message_still_backing_off_is_not_touched():
    """A tick that arrives early must skip it, not burn an attempt."""
    email = Provider(ok=False)
    svc, deliveries, templates, clock = build(email)
    a_template(templates)
    notify(svc)
    before = email.calls

    out = run(svc.drain())                # no time has passed
    assert out["drained"] == 0
    assert email.calls == before


# ── retryable vs permanent ───────────────────────────────────────────────────
def test_a_permanent_error_is_NOT_retried():
    """Retrying a malformed address for ever is not resilience. It is a loop that burns provider
    quota, delays every real message behind it, and arrives at the same answer."""
    email = Provider(error=ValueError("invalid recipient address"))
    svc, deliveries, templates, _ = build(email)
    a_template(templates)

    res = notify(svc)
    assert res["status"] == FAILED, "it will fail identically for ever; do not pay to find out"


def test_a_retryable_error_is_retried():
    email = Provider(error=TimeoutError("timeout talking to provider"))
    svc, deliveries, templates, _ = build(email)
    a_template(templates)
    assert notify(svc)["status"] == QUEUED


def test_permanent_beats_retryable_when_both_match():
    """An operator who lists a token in both means 'do not spend money on this again'."""
    p = policy(retryable=("timeout",), permanent=("timeout",))
    assert p.is_retryable("timeout") is False


def test_an_unclassified_error_follows_the_configured_default():
    assert policy(unknown_is_retryable=True).is_retryable("who knows") is True
    assert policy(unknown_is_retryable=False).is_retryable("who knows") is False


# ── giving up is a DIFFERENT fact from never working ─────────────────────────
def test_it_dead_letters_after_max_attempts_and_stays_findable():
    email = Provider(ok=False)
    svc, deliveries, templates, clock = build(email, retry=policy(max_attempts=3))
    a_template(templates)

    notify(svc)                            # attempt 1 -> queued
    for _ in range(5):
        clock.t += 3600
        run(svc.drain())

    rows = run(deliveries.list(10))
    assert rows[0]["status"] == DEAD_LETTER, \
        "'we gave up' and 'it was never going to work' are different facts"
    assert rows[0]["attempts"] == 3
    assert rows[0]["error"], "and it must say WHY, or nobody can act on it"


def test_dead_letter_is_not_the_same_as_failed():
    """Collapsing them would hide which outages actually cost us messages."""
    p = policy(max_attempts=2)
    assert p.next_state(1, "timeout") == QUEUED
    assert p.next_state(2, "timeout") == DEAD_LETTER        # ran out of patience
    assert p.next_state(1, "invalid number") == FAILED      # never going to work


# ── backoff ──────────────────────────────────────────────────────────────────
def test_backoff_is_jittered_not_a_thundering_herd():
    """Without jitter every message that failed during an outage retries at the SAME instant the
    provider recovers — and we knock it over again ourselves."""
    p = policy(base_ms=1000, max_ms=60_000)
    waits = {p.backoff_ms(3, rand=random.Random(seed)) for seed in range(50)}
    assert len(waits) > 1, "identical waits are a synchronised retry storm"
    assert all(0 <= w <= 4000 for w in waits), "full jitter: a point in [0, exponential]"


def test_backoff_is_capped():
    p = policy(base_ms=1000, max_ms=5000)
    assert all(p.backoff_ms(a) <= 5000 for a in range(1, 20))


def test_zero_attempts_is_refused():
    with pytest.raises(ValueError):
        policy(max_attempts=0)


# ── inline mode still available, eyes open ───────────────────────────────────
def test_inline_mode_keeps_the_old_behaviour():
    email = Provider(ok=False)
    svc, deliveries, templates, _ = build(email, mode=INLINE)
    a_template(templates)
    assert notify(svc)["status"] == FAILED
    assert run(svc.drain())["drained"] == 0


def test_a_successful_send_is_not_queued():
    """The guard must fire only on failure. A working provider must be untouched."""
    svc, deliveries, templates, _ = build(Provider(ok=True))
    a_template(templates)
    res = notify(svc)
    assert res["status"] == SENT
    assert "retry_at" not in res
