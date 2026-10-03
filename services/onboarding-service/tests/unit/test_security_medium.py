"""Security MEDIUM remediation: a token with no sub is 401, not a blank Principal (onboarding-012)."""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException


class _State:
    pass


class _Verifier:
    def __init__(self, claims):
        self._c = claims
    def verify(self, token):
        return self._c


class _App:
    def __init__(self, claims):
        self.state = _State()
        self.state.container = _State()
        self.state.container.token_verifier = _Verifier(claims)


class _Req:
    def __init__(self, claims):
        self.app = _App(claims)
        self.headers = {"authorization": "Bearer good"}
        self.state = _State()


def _run_require(claims):
    import app.api.auth as auth
    return asyncio.run(auth.require_user(_Req(claims)))


def test_empty_sub_is_rejected():
    with pytest.raises(HTTPException) as ei:
        _run_require({"sub": "", "tenant": "t1", "caps": ["x"]})
    assert ei.value.status_code == 401


def test_missing_sub_is_rejected():
    with pytest.raises(HTTPException) as ei:
        _run_require({"tenant": "t1"})
    assert ei.value.status_code == 401


def test_real_sub_is_accepted():
    p = _run_require({"sub": "u1", "tenant": "t1", "caps": ["x"]})
    assert p.subject == "u1"
