"""Send idempotency — a retried notify with the same key must NOT re-send.

Duplicate OTP SMS costs real money; duplicate emails erode trust. At-least-once callers
(a retry after a network blip) must be able to pass a key and get the original result back.
"""
from __future__ import annotations

import asyncio
import pathlib
import re

from app.adapters.datastore.memory import MemoryDataStore
from app.core.notifier import NotificationService
from app.core.stores import DeliveryStore, InAppStore, PreferenceStore, TemplateStore
from app.core.models import Template


def run(c):
    return asyncio.run(c)


class CountingEmail:
    driver = "counting"

    def __init__(self):
        self.calls = 0

    def send(self, to, subject, text, html, sender):
        self.calls += 1
        return True, "counting"


def _svc(email, ttl=86_400):
    st = MemoryDataStore()
    run(TemplateStore(st).upsert(Template(key="welcome", locale="en", subject="Hi", body="Hello")))

    class SMS:
        driver = "s"

        def send(self, to, body):
            return True, "s"

    class R:
        def render(self, subject, body, variables):
            return subject, body

    return NotificationService(templates=TemplateStore(st), prefs=PreferenceStore(st),
                               deliveries=DeliveryStore(st), inapp=InAppStore(st),
                               email=email, sms=SMS(), renderer=R(),
                               email_from="x@y.z", default_locale="en", dedupe_ttl_seconds=ttl), st


def test_same_key_does_not_resend():
    email = CountingEmail()
    svc, _ = _svc(email)
    a = run(svc.notify(recipient="u@x.com", channel="email", template_key="welcome",
                       idempotency_key="evt_1"))
    b = run(svc.notify(recipient="u@x.com", channel="email", template_key="welcome",
                       idempotency_key="evt_1"))
    assert a["status"] == "sent"
    assert b["duplicate"] is True and b["delivery_id"] == a["delivery_id"]
    assert email.calls == 1                       # the provider was hit exactly once


def test_no_key_always_sends():
    email = CountingEmail()
    svc, _ = _svc(email)
    run(svc.notify(recipient="u@x.com", channel="email", template_key="welcome"))
    run(svc.notify(recipient="u@x.com", channel="email", template_key="welcome"))
    assert email.calls == 2                        # no key → no dedupe


def test_different_keys_send_separately():
    email = CountingEmail()
    svc, _ = _svc(email)
    run(svc.notify(recipient="u@x.com", channel="email", template_key="welcome", idempotency_key="a"))
    run(svc.notify(recipient="u@x.com", channel="email", template_key="welcome", idempotency_key="b"))
    assert email.calls == 2


def test_failed_send_is_not_deduped_so_retry_works():
    class FlakyEmail:
        driver = "flaky"

        def __init__(self):
            self.calls = 0

        def send(self, *a):
            self.calls += 1
            return (self.calls > 1), "flaky"       # first fails, retry succeeds

    email = FlakyEmail()
    svc, _ = _svc(email)
    first = run(svc.notify(recipient="u@x.com", channel="email", template_key="welcome",
                           idempotency_key="evt_1"))
    assert first["status"] == "failed"
    second = run(svc.notify(recipient="u@x.com", channel="email", template_key="welcome",
                            idempotency_key="evt_1"))
    assert second["status"] == "sent" and email.calls == 2   # retry went through


def test_same_key_different_tenants_do_not_collide():
    """Two tenants presenting the SAME caller key both send — the dedupe slot is tenant-scoped.

    Before scoping, tenant B's send with tenant A's key was told `duplicate` and handed A's result,
    so B's message (an OTP, a receipt) never went out — a cross-tenant dropped send.
    """
    email = CountingEmail()
    svc, _ = _svc(email)
    a = run(svc.notify(recipient="a@x.com", channel="email", template_key="welcome",
                       tenant="acme", idempotency_key="order-123"))
    b = run(svc.notify(recipient="b@x.com", channel="email", template_key="welcome",
                       tenant="globex", idempotency_key="order-123"))
    assert a["status"] == "sent" and b["status"] == "sent"
    assert "duplicate" not in b and email.calls == 2      # B was NOT suppressed by A's key
    # ...and A's own retry is still deduped within its own tenant.
    a2 = run(svc.notify(recipient="a@x.com", channel="email", template_key="welcome",
                        tenant="acme", idempotency_key="order-123"))
    assert a2.get("duplicate") is True and email.calls == 2


def test_dedupe_window_expires():
    email = CountingEmail()
    svc, st = _svc(email, ttl=3600)
    run(svc.notify(recipient="u@x.com", channel="email", template_key="welcome", idempotency_key="evt_1"))
    # force the remembered record's timestamp into the past. The dedupe record is tenant-scoped
    # (length-prefixed), so with no tenant the stored key is "0:|evt_1", not the raw "evt_1".
    from app.core.stores import DeliveryStore
    scoped = DeliveryStore._idempo_key("", "evt_1")
    row = run(st.get("notify_idempo", scoped))
    row["ts"] = 0
    run(st.put("notify_idempo", scoped, row))
    out = run(svc.notify(recipient="u@x.com", channel="email", template_key="welcome", idempotency_key="evt_1"))
    assert "duplicate" not in out and email.calls == 2       # window elapsed → sends again


def test_every_settings_field_is_consumed_somewhere():
    from app.config import Settings

    root = pathlib.Path(__file__).resolve().parents[2]
    files = [p for p in root.rglob("*") if p.is_file() and p.suffix in (".py", ".toml", "")
             and "tests" not in p.parts and ".venv" not in p.parts and "__pycache__" not in p.parts]
    lines: list[str] = []
    for p in files:
        try:
            lines.extend(p.read_text(encoding="utf-8").splitlines())
        except (UnicodeDecodeError, OSError):
            continue
    unused = []
    for name in Settings.model_fields:
        decl = re.compile(rf"^\s*{re.escape(name)}\s*:")
        if not [ln for ln in lines if re.search(rf"\b{re.escape(name)}\b", ln) and not decl.match(ln)]:
            unused.append(name)
    assert not unused, f"config knobs declared but never consumed: {unused}"
