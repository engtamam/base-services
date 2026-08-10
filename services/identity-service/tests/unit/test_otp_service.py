"""OTP primitive — the three properties the reporter named (TTL, attempt limit, per-identifier rate
limit) plus the four that make it safe: hashed-at-rest, single-use, constant-time, unguessable."""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.cache.memory import MemoryCache
from app.core.otp_service import OtpError, OtpService


def run(c):
    return asyncio.run(c)


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _svc(clock=None, **kw):
    return OtpService(cache=MemoryCache(), salt="pepper", clock=clock or _Clock(), **kw)


def test_a_correct_code_verifies_exactly_once():
    svc = _svc()
    code = run(svc.issue(identifier="+966501234567"))["code"]
    assert run(svc.verify(identifier="+966501234567", code=code))["verified"] is True
    with pytest.raises(OtpError) as e:                      # consumed — cannot replay
        run(svc.verify(identifier="+966501234567", code=code))
    assert e.value.code == "NO_CODE"


def test_a_wrong_code_is_refused_and_burns_an_attempt():
    svc = _svc(max_attempts=3)
    run(svc.issue(identifier="a@b.com"))
    for _ in range(3):
        with pytest.raises(OtpError) as e:
            run(svc.verify(identifier="a@b.com", code="000000"))
        assert e.value.code == "INVALID"
    with pytest.raises(OtpError) as e:                      # 4th try: exhausted, code destroyed
        run(svc.verify(identifier="a@b.com", code="000000"))
    assert e.value.code == "TOO_MANY_ATTEMPTS"


def test_the_code_expires_after_its_ttl():
    clk = _Clock()
    svc = _svc(clock=clk, ttl_seconds=300)
    code = run(svc.issue(identifier="x"))["code"]
    clk.t += 301
    with pytest.raises(OtpError) as e:
        run(svc.verify(identifier="x", code=code))
    assert e.value.code == "EXPIRED"


def test_only_a_hash_is_stored_never_the_raw_code():
    cache = MemoryCache()
    svc = OtpService(cache=cache, salt="pepper", clock=_Clock())
    code = run(svc.issue(identifier="x"))["code"]
    assert code not in str(cache._data)                    # a leaked store is useless


def test_resend_cooldown_blocks_a_rapid_second_send():
    clk = _Clock()
    svc = _svc(clock=clk, resend_cooldown_seconds=60)
    run(svc.issue(identifier="x"))
    with pytest.raises(OtpError) as e:
        run(svc.issue(identifier="x"))
    assert e.value.code == "RESEND_TOO_SOON"
    clk.t += 61
    assert run(svc.issue(identifier="x"))["code"]           # allowed once the cooldown passes


def test_hourly_cap_stops_an_sms_bomb():
    clk = _Clock()
    svc = _svc(clock=clk, resend_cooldown_seconds=0, max_per_hour=3)
    for _ in range(3):
        run(svc.issue(identifier="x"))
    with pytest.raises(OtpError) as e:
        run(svc.issue(identifier="x"))
    assert e.value.code == "RATE_LIMITED"


def test_the_hourly_window_slides():
    clk = _Clock()
    svc = _svc(clock=clk, resend_cooldown_seconds=0, max_per_hour=2)
    run(svc.issue(identifier="x"))
    run(svc.issue(identifier="x"))
    clk.t += 3601
    assert run(svc.issue(identifier="x"))["code"]           # a new hour, a fresh budget


def test_rate_limit_is_per_identifier():
    clk = _Clock()
    svc = _svc(clock=clk, resend_cooldown_seconds=60)
    run(svc.issue(identifier="a"))
    assert run(svc.issue(identifier="b"))["code"]           # b is not throttled by a


def test_codes_are_the_configured_length_and_numeric():
    svc = _svc(code_length=8)
    code = run(svc.issue(identifier="x"))["code"]
    assert len(code) == 8 and code.isdigit()


def test_purposes_are_isolated():
    svc = _svc(resend_cooldown_seconds=0)
    login = run(svc.issue(identifier="x", purpose="login"))["code"]
    run(svc.issue(identifier="x", purpose="reset"))
    # a login code must not satisfy a reset challenge
    with pytest.raises(OtpError):
        run(svc.verify(identifier="x", code=login, purpose="reset"))
    assert run(svc.verify(identifier="x", code=login, purpose="login"))["verified"]
