"""Integration: POST /api/v1/auth/reset-password — OTP-verified account recovery, no old password.

The load-bearing behaviours: a verified reset code sets a new password (old stops working, new works);
a wrong code and an unknown identifier are INDISTINGUISHABLE (no enumeration oracle); and a weak
password is rejected WITHOUT consuming the single-use code, so a locked-out admin can retry.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

ITOKEN = "t" * 40
SECRET = "h" * 40
PHONE = "+966500000001"
OLD = "oldpass123"


class _CapturingNotifier:
    driver = "capture"

    def __init__(self):
        self.last = None

    async def send(self, *, to, template, tenant, variables=None, channel="email"):
        self.last = {"to": to, "variables": variables or {}, "channel": channel}
        return True


def _make():
    from app.config import get_settings
    os.environ.update(
        ENV="test", INTERNAL_SERVICE_TOKEN=ITOKEN, IDENTITY_MULTI_TENANT="true",
        PLATFORM_TENANT="platform",
        IDENTITY_LOGIN_FIELD="phone", OTP_TEMPLATE="otp_reset", RATELIMIT_MAX="1000",
        TOKEN_ISSUER_DRIVER="hs256", TOKEN_SIGNING_SECRET=SECRET,
        TOKEN_VERIFIER_DRIVER="hs256", TOKEN_HS256_SECRET=SECRET)
    get_settings.cache_clear()
    from app.main import create_app
    app = create_app()
    cap = _CapturingNotifier()
    app.state.container.notifier = cap      # read the code the human would receive
    return TestClient(app, raise_server_exceptions=False), cap


@pytest.fixture
def client_cap():
    saved = dict(os.environ)
    c, cap = _make()
    yield c, cap
    os.environ.clear()
    os.environ.update(saved)
    from app.config import get_settings
    get_settings.cache_clear()


def _seed(c, identifier=PHONE, password=OLD):
    return c.post("/internal/tenancy/seed-owner",
                  json={"tenant": "shop", "identifier": identifier, "caps": ["members:write"],
                        "password": password}, headers={"X-Internal-Token": ITOKEN})


def _request_code(c, cap, identifier=PHONE):
    r = c.post("/api/v1/otp/request", json={"identifier": identifier, "purpose": "reset"})
    assert r.status_code == 200
    return cap.last["variables"]["code"]


def _person_login(c, identifier, password):
    return c.post("/api/v1/auth/person/login", json={"username": identifier, "password": password})


def test_reset_password_end_to_end(client_cap):
    c, cap = client_cap
    assert _seed(c).status_code in (200, 201)
    assert _person_login(c, PHONE, OLD).status_code == 200          # old works before reset
    code = _request_code(c, cap)
    r = c.post("/api/v1/auth/reset-password",
               json={"identifier": PHONE, "code": code, "new_password": "brandnew456"})
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert _person_login(c, PHONE, "brandnew456").status_code == 200   # new works
    assert _person_login(c, PHONE, OLD).status_code == 401             # old no longer works


def test_wrong_code_is_generic_and_changes_nothing(client_cap):
    c, cap = client_cap
    _seed(c)
    _request_code(c, cap)
    r = c.post("/api/v1/auth/reset-password",
               json={"identifier": PHONE, "code": "000000", "new_password": "brandnew456"})
    assert r.status_code == 400 and r.json()["error"] == "INVALID_RESET"
    assert _person_login(c, PHONE, OLD).status_code == 200          # unchanged


def test_unknown_identifier_is_indistinguishable_from_a_bad_code(client_cap):
    c, cap = client_cap
    other = "+966500000099"                                         # no account seeded
    c.post("/api/v1/otp/request", json={"identifier": other, "purpose": "reset"})
    code = cap.last["variables"]["code"]
    r = c.post("/api/v1/auth/reset-password",
               json={"identifier": other, "code": code, "new_password": "brandnew456"})
    assert r.status_code == 400 and r.json()["error"] == "INVALID_RESET"   # no oracle


def test_weak_password_is_rejected_without_consuming_the_code(client_cap):
    c, cap = client_cap
    _seed(c)
    code = _request_code(c, cap)
    weak = c.post("/api/v1/auth/reset-password",
                  json={"identifier": PHONE, "code": code, "new_password": "x"})
    assert weak.status_code == 400 and weak.json()["error"] == "WEAK_PASSWORD"
    # the code was NOT spent — a strong retry with the SAME code still succeeds.
    ok = c.post("/api/v1/auth/reset-password",
                json={"identifier": PHONE, "code": code, "new_password": "brandnew456"})
    assert ok.status_code == 200
    assert _person_login(c, PHONE, "brandnew456").status_code == 200


def test_a_consumed_code_cannot_be_replayed(client_cap):
    c, cap = client_cap
    _seed(c)
    code = _request_code(c, cap)
    assert c.post("/api/v1/auth/reset-password",
                  json={"identifier": PHONE, "code": code, "new_password": "brandnew456"}
                  ).status_code == 200
    # same code again → already consumed → generic INVALID_RESET
    assert c.post("/api/v1/auth/reset-password",
                  json={"identifier": PHONE, "code": code, "new_password": "another789"}
                  ).status_code == 400
