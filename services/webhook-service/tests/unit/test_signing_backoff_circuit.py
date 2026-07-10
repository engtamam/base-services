"""Signing (replay + rotation + constant-time), backoff (jitter), circuit breaker."""
from __future__ import annotations

import statistics

from app.core import backoff, circuit, signing


# ── signing ───────────────────────────────────────────────────────────────────
def test_sign_verify_roundtrip():
    body = b'{"a":1}'
    header = signing.sign(body, "sec", 1_700_000_000)
    assert header.startswith("t=1700000000,v1=")
    assert signing.verify(body, header, ["sec"], now=1_700_000_000)


def test_signature_covers_the_body():
    header = signing.sign(b'{"a":1}', "sec", 1000)
    assert not signing.verify(b'{"a":2}', header, ["sec"], now=1000)


def test_replay_outside_tolerance_rejected():
    header = signing.sign(b"x", "sec", 1000)
    assert signing.verify(b"x", header, ["sec"], now=1200, tolerance_seconds=300)
    assert not signing.verify(b"x", header, ["sec"], now=1400, tolerance_seconds=300)   # stale
    assert not signing.verify(b"x", header, ["sec"], now=600, tolerance_seconds=300)    # future


def test_wrong_secret_rejected_and_rotation_accepts_both():
    header_old = signing.sign(b"x", "old", 1000)
    assert not signing.verify(b"x", header_old, ["new"], now=1000)
    assert signing.verify(b"x", header_old, ["new", "old"], now=1000)    # during rotation


def test_malformed_headers_rejected():
    for header in ("", "garbage", "t=abc,v1=ff", "v1=ff", "t=1000"):
        assert not signing.verify(b"x", header, ["sec"], now=1000)


def test_no_secrets_never_verifies():
    header = signing.sign(b"x", "sec", 1000)
    assert not signing.verify(b"x", header, [], now=1000)
    assert not signing.verify(b"x", header, [""], now=1000)


# ── backoff ───────────────────────────────────────────────────────────────────
def test_backoff_exhaustion_returns_none():
    assert backoff.next_delay(3, base_seconds=30, max_seconds=3600, max_attempts=3) is None


def test_backoff_without_jitter_is_exponential_and_capped():
    delays = [backoff.next_delay(n, base_seconds=10, max_seconds=100, max_attempts=10, jitter=False)
              for n in range(5)]
    assert delays == [10.0, 20.0, 40.0, 80.0, 100.0]        # capped at max_seconds


def test_full_jitter_spreads_retries_and_stays_in_range():
    """Plain 2^n synchronises every failed delivery into a thundering herd."""
    ceiling = 30 * (2 ** 3)
    delays = [backoff.next_delay(3, base_seconds=30, max_seconds=10_000, max_attempts=8,
                                 seed=f"dlv_{i}") for i in range(500)]
    assert all(0 <= d <= ceiling for d in delays)
    assert len(set(delays)) > 400                            # not synchronised
    assert 0.35 * ceiling < statistics.mean(delays) < 0.65 * ceiling   # ~uniform(0, ceiling)


def test_jitter_is_deterministic_per_delivery_and_attempt():
    a = backoff.next_delay(2, base_seconds=30, max_seconds=3600, max_attempts=8, seed="dlv_1")
    b = backoff.next_delay(2, base_seconds=30, max_seconds=3600, max_attempts=8, seed="dlv_1")
    c = backoff.next_delay(3, base_seconds=30, max_seconds=3600, max_attempts=8, seed="dlv_1")
    assert a == b and a != c


# ── circuit breaker ───────────────────────────────────────────────────────────
def test_circuit_opens_after_threshold():
    state = circuit.CLOSED
    failures = 0
    for _ in range(2):
        out = circuit.on_failure(consecutive_failures=failures, state=state, now=100, threshold=3)
        state, failures = out["circuit_state"], out["consecutive_failures"]
    assert state == circuit.CLOSED
    out = circuit.on_failure(consecutive_failures=failures, state=state, now=100, threshold=3)
    assert out["circuit_state"] == circuit.OPEN and out["opened_at"] == 100


def test_open_becomes_half_open_after_reset_then_closes_on_success():
    assert circuit.state_now(circuit_state="open", opened_at=100, now=200, reset_seconds=300) == circuit.OPEN
    assert circuit.state_now(circuit_state="open", opened_at=100, now=500, reset_seconds=300) == circuit.HALF_OPEN
    assert not circuit.can_attempt(circuit.OPEN)
    assert circuit.can_attempt(circuit.HALF_OPEN) and circuit.can_attempt(circuit.CLOSED)
    assert circuit.on_success(consecutive_failures=9)["circuit_state"] == circuit.CLOSED


def test_failed_half_open_probe_reopens_immediately():
    out = circuit.on_failure(consecutive_failures=0, state=circuit.HALF_OPEN, now=500, threshold=99)
    assert out["circuit_state"] == circuit.OPEN and out["opened_at"] == 500
