"""Security MEDIUM remediation: OTP attempt-reset on re-issue (identity-006) and the OTP_SALT boot
guard (identity-010)."""
from __future__ import annotations

import asyncio
import json

import pytest

from app.core.otp_service import OtpService


class _Cache:
    driver = "memory"

    def __init__(self):
        self.d = {}

    async def get(self, k):
        return self.d.get(k)

    async def set(self, k, v, ttl=None):
        self.d[k] = v

    async def delete(self, k):
        self.d.pop(k, None)

    async def incr(self, k, ttl=None):
        self.d[k] = str(int(self.d.get(k, "0")) + 1)
        return int(self.d[k])


def run(c):
    return asyncio.run(c)


# ── identity-006: re-issuing a code clears the attempt counter ───────────────────
def test_reissue_resets_attempt_counter():
    cache = _Cache()
    t = [1000.0]
    svc = OtpService(cache=cache, salt="s" * 16, clock=lambda: t[0], max_attempts=5,
                     resend_cooldown_seconds=0)
    run(svc.issue(identifier="+966500000000", purpose="reset"))
    # burn two attempts with wrong codes (verify raises OtpError on a wrong code)
    from app.core.otp_service import OtpError
    for _ in range(2):
        try:
            run(svc.verify(identifier="+966500000000", code="000000", purpose="reset"))
        except OtpError:
            pass
    assert int(cache.d.get("otp:reset:+966500000000:attempts", "0")) >= 1
    t[0] += 1
    run(svc.issue(identifier="+966500000000", purpose="reset"))   # resend
    assert "otp:reset:+966500000000:attempts" not in cache.d      # fresh code, clean budget


# ── identity-010: OTP_SALT boot guard ────────────────────────────────────────────
def _settings(**over):
    from app.config import Settings
    base = dict(_env_file=None, INTERNAL_SERVICE_TOKEN="z" * 40, PII_SALT="p" * 16)
    base.update(over)
    return Settings(**base)


def test_otp_salt_required_in_production():
    with pytest.raises(ValueError):
        _settings(ENV="production", OTP_SALT="").assert_otp_salt_ok()


def test_otp_salt_must_differ_from_pii_salt():
    with pytest.raises(ValueError):
        _settings(ENV="production", OTP_SALT="p" * 16).assert_otp_salt_ok()


def test_otp_salt_distinct_is_fine():
    _settings(ENV="production", OTP_SALT="o" * 16).assert_otp_salt_ok()


def test_otp_salt_fallback_fine_in_dev():
    _settings(ENV="test", OTP_SALT="").assert_otp_salt_ok()
