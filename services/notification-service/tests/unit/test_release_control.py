"""Meta Business Agent handover — RELEASE thread control after we send to a `meta`-mode number.

Meta's protocol: our outbound send TAKES control from the agent and never auto-returns it, so a
meta-mode number must release after each send or the customer is left with a muted agent and no reply.
This covers the release service (mode gating, durability, retry, dead-lettering) and the notifier hook.
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.notifier import NotificationService
from app.core.release_control import ReleaseControlService
from app.core.retry import RetryPolicy
from app.core.stores import (DeliveryStore, HandoverModeStore, InAppStore, PreferenceStore,
                             ReleaseControlStore, TemplateStore)

_COLL = "wa_release_control"


def run(c):
    return asyncio.run(c)


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def now(self):
        return self.t


class FakeWA:
    """A WhatsApp provider double: records release calls and can fail the first `fail_times` of them."""
    driver = "meta"

    def __init__(self, phone_number_id="PID1", fail_times=0):
        self.phone_number_id = phone_number_id
        self.releases: list[str] = []
        self._fail = fail_times

    def release_thread_control(self, to, metadata=""):
        self.releases.append(to)
        if self._fail > 0:
            self._fail -= 1
            return False, "HTTP 503: busy"
        return True, ""

    # send_interactive success path for the integration test. `self._whatsapp` in the notifier is the
    # failover-shaped object, so this returns the (ok, provider, wamid) tuple, not a raw wamid string.
    def send_interactive(self, to, interactive, biz_opaque_callback_data=""):
        return True, self.driver, "wamid:test"


def _svc(store, *, enabled=True, default_mode="meta", max_attempts=5, clock=None):
    return ReleaseControlService(
        store=ReleaseControlStore(store), handover=HandoverModeStore(store),
        default_mode=default_mode, enabled=enabled,
        retry=RetryPolicy(max_attempts=max_attempts, base_ms=1000, max_ms=60_000,
                          retryable=(), permanent=(), unknown_is_retryable=True),
        clock=(clock or (lambda: 1000.0)))


def _rows(store):
    return run(store.scan_all(_COLL, limit=50))


# ── mode gating ──────────────────────────────────────────────────────────────────
def test_meta_mode_send_releases_control_and_drops_the_ephemeral_row():
    st = MemoryDataStore()
    wa = FakeWA()
    run(_svc(st).after_send(wa=wa, recipient="966500", tenant="acme"))
    assert wa.releases == ["966500"]          # released back to the agent
    assert _rows(st) == []                     # success → the ephemeral row is deleted


def test_us_mode_number_keeps_control_and_never_releases():
    st = MemoryDataStore()
    run(HandoverModeStore(st).set(phone_number_id="PID1", mode="us"))
    wa = FakeWA()
    run(_svc(st).after_send(wa=wa, recipient="x", tenant="t"))
    assert wa.releases == [] and _rows(st) == []


def test_per_number_us_override_beats_a_meta_default():
    st = MemoryDataStore()
    run(HandoverModeStore(st).set(phone_number_id="PID1", mode="us"))   # this number opted out
    wa = FakeWA()
    run(_svc(st, default_mode="meta").after_send(wa=wa, recipient="x", tenant="t"))
    assert wa.releases == []


def test_disabled_feature_is_a_total_noop():
    st = MemoryDataStore()
    wa = FakeWA()
    run(_svc(st, enabled=False).after_send(wa=wa, recipient="x", tenant="t"))
    assert wa.releases == [] and _rows(st) == []


def test_a_chain_with_no_meta_number_is_skipped():
    st = MemoryDataStore()
    wa = FakeWA(phone_number_id="")            # console/webhook-only chain
    run(_svc(st).after_send(wa=wa, recipient="x", tenant="t"))
    assert wa.releases == [] and _rows(st) == []


# ── durability: failure → queued → drained ────────────────────────────────────────
def test_a_failed_release_is_queued_and_then_drained_to_success():
    clk = _Clock()
    st = MemoryDataStore()
    svc = _svc(st, clock=clk.now)
    wa = FakeWA(fail_times=1)                   # first attempt fails, second succeeds
    run(svc.after_send(wa=wa, recipient="9", tenant="t"))
    rows = _rows(st)
    assert len(rows) == 1 and rows[0]["status"] == "queued" and rows[0]["attempts"] == 1

    clk.t += 10_000                            # past the backoff

    async def resolver(pid, tenant):
        assert pid == "PID1"
        return wa

    out = run(svc.drain(resolver, limit=10))
    assert out["released"] == 1
    assert _rows(st) == []                      # released on retry → row gone


def test_a_release_not_yet_due_is_skipped_by_drain():
    clk = _Clock()
    st = MemoryDataStore()
    svc = _svc(st, clock=clk.now)
    wa = FakeWA(fail_times=1)
    run(svc.after_send(wa=wa, recipient="9", tenant="t"))   # queued with a future next_attempt_ts

    async def resolver(pid, tenant):
        return wa

    out = run(svc.drain(resolver, limit=10))   # no clock advance → still backing off
    assert out["drained"] == 0 and _rows(st)[0]["status"] == "queued"


def test_release_goes_dead_after_exhausting_retries_never_silently_dropped():
    clk = _Clock()
    st = MemoryDataStore()
    svc = _svc(st, max_attempts=2, clock=clk.now)
    wa = FakeWA(fail_times=99)                  # never succeeds
    run(svc.after_send(wa=wa, recipient="9", tenant="t"))   # attempt 1 → queued
    clk.t += 10_000

    async def resolver(pid, tenant):
        return wa

    out = run(svc.drain(resolver, limit=10))   # attempt 2 → dead (>= max_attempts)
    assert out["dead"] == 1
    rows = _rows(st)
    assert len(rows) == 1 and rows[0]["status"] == "dead" and rows[0]["attempts"] == 2  # kept, not lost


def test_drain_leaves_the_row_queued_when_credentials_cannot_be_resolved():
    clk = _Clock()
    st = MemoryDataStore()
    svc = _svc(st, clock=clk.now)
    run(svc.after_send(wa=FakeWA(fail_times=1), recipient="9", tenant="t"))
    clk.t += 10_000

    async def resolver(pid, tenant):
        return None                            # e.g. the number's credentials are unavailable now

    out = run(svc.drain(resolver, limit=10))
    assert out["requeued"] == 1 and _rows(st)[0]["status"] == "queued"


# ── the notifier hook ──────────────────────────────────────────────────────────────
class _Renderer:
    def render(self, s, b, v):
        return s, b


def test_notifier_releases_after_a_successful_meta_mode_interactive_send():
    st = MemoryDataStore()
    wa = FakeWA()
    release = _svc(st)
    svc = NotificationService(
        templates=TemplateStore(st), prefs=PreferenceStore(st),
        deliveries=DeliveryStore(st), inapp=InAppStore(st, page_size=20, scan_limit=100),
        email=None, sms=None, renderer=_Renderer(), email_from="x@y", default_locale="en",
        whatsapp=wa, release_control=release)
    res = run(svc.send_interactive(recipient="966777", tenant="acme",
                                   interactive={"type": "button"}, kind="order_status"))
    assert res["status"] == "sent"
    assert wa.releases == ["966777"]           # the send handed control back to the agent
    assert _rows(st) == []                      # released inline → nothing left queued
