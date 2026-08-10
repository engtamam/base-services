"""OTP — issue and verify a one-time code against a claimed identifier (a phone, an email, anything).

The three things that separate an OTP from a nuisance, and the reporter named all three: a stated TTL,
an attempt limit, and rate limiting per identifier. Channel-AGNOSTIC — this owns issue/verify; the
DELIVERY (SMS, WhatsApp, email) is somebody else's transport, so the same primitive proves a phone,
an email, or a number claimed at a counter.

Only a SALTED HASH of the code is stored (a leaked store is useless), the compare is constant-time,
the code is single-use (consumed on the first correct match), and a wrong code burns an attempt so a
6-digit code cannot be brute-forced within its short life.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from typing import Any


def _now() -> float:
    return time.time()


class OtpError(Exception):
    """A wrong/expired/exhausted code, or too many requests. Never reveals which to a caller who
    should not learn it — the route maps it to one generic 'invalid or expired code'."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


class OtpService:
    def __init__(self, *, cache, salt: str, clock=_now, code_length: int = 6,
                 ttl_seconds: int = 300, max_attempts: int = 5,
                 resend_cooldown_seconds: int = 60, max_per_hour: int = 5) -> None:
        self._cache = cache
        self._salt = salt or ""
        self._clock = clock
        self._len = max(4, int(code_length))
        self._ttl = int(ttl_seconds)
        self._max_attempts = max(1, int(max_attempts))
        self._cooldown = int(resend_cooldown_seconds)
        self._per_hour = max(1, int(max_per_hour))

    # ── keys ──────────────────────────────────────────────────────────────────────
    @staticmethod
    def _key(purpose: str, identifier: str) -> str:
        return f"otp:{purpose}:{identifier}"

    @staticmethod
    def _rl_key(purpose: str, identifier: str) -> str:
        return f"otp_rl:{purpose}:{identifier}"

    def _hash(self, code: str) -> str:
        return hmac.new(self._salt.encode(), code.encode(), hashlib.sha256).hexdigest()

    def _random_code(self) -> str:
        # Uniformly random digits; secrets, never a predictable PRNG.
        return "".join(str(secrets.randbelow(10)) for _ in range(self._len))

    # ── issue ─────────────────────────────────────────────────────────────────────
    async def issue(self, *, identifier: str, purpose: str = "verify") -> dict[str, Any]:
        """Mint a code and store its HASH under a TTL. Returns the RAW code exactly once for the
        caller to deliver. Rate-limited PER identifier: a resend cooldown and an hourly cap, so a
        number cannot be spammed with texts."""
        if not identifier:
            raise OtpError("NO_IDENTIFIER")
        now = self._clock()
        rl_raw = await self._cache.get(self._rl_key(purpose, identifier))
        rl = json.loads(rl_raw) if rl_raw else {"count": 0, "window_start": now, "last": 0.0}
        # Slide the hourly window.
        if now - float(rl.get("window_start", now)) >= 3600:
            rl = {"count": 0, "window_start": now, "last": rl.get("last", 0.0)}
        if now - float(rl.get("last", 0.0)) < self._cooldown:
            raise OtpError("RESEND_TOO_SOON")
        if int(rl.get("count", 0)) >= self._per_hour:
            raise OtpError("RATE_LIMITED")

        code = self._random_code()
        record = {"hash": self._hash(code), "expires_at": now + self._ttl,
                  "attempts": 0, "issued_at": now}
        await self._cache.set(self._key(purpose, identifier), json.dumps(record), self._ttl)
        rl = {"count": int(rl.get("count", 0)) + 1, "window_start": rl.get("window_start", now),
              "last": now}
        await self._cache.set(self._rl_key(purpose, identifier), json.dumps(rl), 3600)
        return {"code": code, "expires_in": self._ttl, "length": self._len}

    # ── verify ────────────────────────────────────────────────────────────────────
    async def verify(self, *, identifier: str, code: str, purpose: str = "verify") -> dict[str, Any]:
        """Consume the code. A correct match verifies once and deletes it; a wrong code burns an
        attempt and, past the limit, deletes the code so it cannot be ground down."""
        key = self._key(purpose, identifier)
        raw = await self._cache.get(key)
        if not raw:
            raise OtpError("NO_CODE")                     # never issued, or already expired/consumed
        rec = json.loads(raw)
        if self._clock() >= float(rec.get("expires_at", 0)):
            await self._cache.delete(key)
            raise OtpError("EXPIRED")
        rec["attempts"] = int(rec.get("attempts", 0)) + 1
        if rec["attempts"] > self._max_attempts:
            await self._cache.delete(key)                 # exhausted — a new code must be issued
            raise OtpError("TOO_MANY_ATTEMPTS")
        if not hmac.compare_digest(self._hash(str(code or "")), str(rec.get("hash", ""))):
            # Persist the incremented attempt (bounded by the record's remaining TTL) then refuse.
            ttl = max(1, int(float(rec["expires_at"]) - self._clock()))
            await self._cache.set(key, json.dumps(rec), ttl)
            raise OtpError("INVALID")
        await self._cache.delete(key)                     # single-use: consumed on success
        return {"verified": True, "identifier": identifier, "purpose": purpose}
