"""Provenance + integrity for a push dispatch — an HMAC signature the receiver verifies.

A push is job-service calling a peer's internal webhook unprompted. The peer must be able to prove
three things before it acts on the call:

  1. it really came from a holder of the push secret (not an arbitrary internet client);
  2. the body was not altered in flight (the signature covers the EXACT bytes);
  3. it is not a REPLAY of an earlier genuine push (the signature covers a timestamp, and the
     receiver rejects one outside a small window).

Scheme (Stripe-webhook style, deliberately boring so a receiver in any language can reproduce it):
  signed = f"{timestamp}.{body}"                       # body = the exact request bytes
  signature = hex( HMAC_SHA256(secret, signed) )
sent as two headers (see dispatcher). The receiver recomputes and compares in CONSTANT TIME.

This is INTEGRITY + REPLAY + shared-secret provenance. It does NOT by itself prove the caller is
job-service *specifically* rather than another holder of the shared secret — that stronger,
per-issuer proof is the RS256 X-Internal-Assertion the dispatcher also sends (verified by the
receiver's existing internal-caller driver). The two are complementary: the assertion names the
issuer; this HMAC binds the exact payload and a timestamp the assertion does not cover.
"""
from __future__ import annotations

import hashlib
import hmac

# Header names — part of the wire contract a receiver reads.
SIGNATURE_HEADER = "X-Job-Signature"
TIMESTAMP_HEADER = "X-Job-Signature-Timestamp"


def sign(secret: str, timestamp: int, body: bytes) -> str:
    """The hex HMAC-SHA256 of `f"{timestamp}.{body}"` under `secret`. Empty when no secret is set —
    an unsigned push (the caller has no secret configured); the receiver decides whether to require
    one."""
    if not secret:
        return ""
    signed = f"{int(timestamp)}.".encode("utf-8") + (body or b"")
    return hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()


def verify(secret: str, timestamp: str | int, body: bytes, signature: str, *,
           now: float, max_skew_seconds: int) -> bool:
    """True iff `signature` matches `body`+`timestamp` under `secret` AND the timestamp is within
    `max_skew_seconds` of `now`. Constant-time compare (CWE-208). Any missing/garbage input → False,
    never an exception — a verifier must fail closed, not crash. This is the receiver-side helper; it
    is exported so a peer service (and our own tests) can verify a push without re-deriving the scheme.
    """
    if not secret or not signature:
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    # Replay window FIRST: an old (or far-future) timestamp is refused even if the HMAC is valid, so
    # a captured genuine push cannot be re-sent later.
    if abs(float(now) - ts) > int(max_skew_seconds):
        return False
    expected = sign(secret, ts, body or b"")
    return hmac.compare_digest(expected, str(signature))
