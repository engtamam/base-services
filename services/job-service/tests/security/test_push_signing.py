"""The push signature — integrity, replay window, and fail-closed on every malformed input.

This is the primitive a receiver uses to refuse an impersonated or replayed push, so its refusals
matter more than its acceptances: any missing/garbage input must return False, never raise.
"""
from __future__ import annotations

from app.core import push_signing

SECRET = "k" * 40
NOW = 1_000_000.0


def _sig(body=b'{"job":1}', ts=int(NOW)):
    return push_signing.sign(SECRET, ts, body), ts, body


def test_a_valid_signature_within_the_window_passes():
    sig, ts, body = _sig()
    assert push_signing.verify(SECRET, ts, body, sig, now=NOW, max_skew_seconds=300)


def test_a_tampered_body_fails():
    sig, ts, body = _sig()
    assert not push_signing.verify(SECRET, ts, body + b" ", sig, now=NOW, max_skew_seconds=300)


def test_a_wrong_secret_fails():
    sig, ts, body = _sig()
    assert not push_signing.verify("other", ts, body, sig, now=NOW, max_skew_seconds=300)


def test_an_expired_or_future_timestamp_fails_even_with_a_valid_hmac():
    sig, ts, body = _sig()
    assert not push_signing.verify(SECRET, ts, body, sig, now=NOW + 301, max_skew_seconds=300)
    assert not push_signing.verify(SECRET, ts, body, sig, now=NOW - 301, max_skew_seconds=300)
    # exactly at the edge is still valid
    assert push_signing.verify(SECRET, ts, body, sig, now=NOW + 300, max_skew_seconds=300)


def test_fails_closed_on_missing_or_garbage_input():
    sig, ts, body = _sig()
    assert not push_signing.verify(SECRET, ts, body, "", now=NOW, max_skew_seconds=300)      # no sig
    assert not push_signing.verify("", ts, body, sig, now=NOW, max_skew_seconds=300)         # no secret
    assert not push_signing.verify(SECRET, "notanint", body, sig, now=NOW, max_skew_seconds=300)
    assert not push_signing.verify(SECRET, None, body, sig, now=NOW, max_skew_seconds=300)


def test_sign_is_empty_without_a_secret():
    assert push_signing.sign("", int(NOW), b"x") == ""
