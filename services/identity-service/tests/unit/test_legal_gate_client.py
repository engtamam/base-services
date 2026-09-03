"""Unit tests for the ComplianceLegalGate HTTP adapter itself — the parsing/failure edges the
integration tests (which use a fake gate) do not exercise. A malformed or unreachable answer must
NEVER be read as "owes nothing": that would silently disable the whole enforcement.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.legal_gate.compliance import ComplianceLegalGate
from app.ports.errors import ConfigError
from app.ports.legal_gate import LegalGateUnavailable


def run(c):
    return asyncio.run(c)


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._p = payload

    def json(self):
        if isinstance(self._p, Exception):
            raise self._p
        return self._p


class _Client:
    def __init__(self, resp=None, exc=None, counter=None):
        self._resp, self._exc, self._counter = resp, exc, counter

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None, params=None):
        if self._counter is not None:
            self._counter.append((url, params))
        if self._exc is not None:
            raise self._exc
        return self._resp


class _Httpx:
    def __init__(self, resp=None, exc=None, counter=None):
        self._resp, self._exc, self._counter = resp, exc, counter

    def AsyncClient(self, timeout=None):
        return _Client(self._resp, self._exc, self._counter)


def _gate(ttl=0):
    g = ComplianceLegalGate(
        "http://compliance", "tok", timeout_ms=1000, ttl_seconds=ttl,
        path="internal/compliance/legal/outstanding", token_header="X-Internal-Token",
        outstanding_field="outstanding", subject_param="subject", tenant_param="tenant")
    return g


def test_config_refuses_without_url_or_token():
    with pytest.raises(ConfigError):
        ComplianceLegalGate("", "tok", timeout_ms=1, ttl_seconds=0, path="p",
                            token_header="H", outstanding_field="outstanding",
                            subject_param="subject", tenant_param="tenant")
    with pytest.raises(ConfigError):
        ComplianceLegalGate("http://x", "", timeout_ms=1, ttl_seconds=0, path="p",
                            token_header="H", outstanding_field="outstanding",
                            subject_param="subject", tenant_param="tenant")


def test_parses_an_owed_list():
    g = _gate()
    g._httpx = _Httpx(_Resp(200, {"outstanding": [{"doc_id": "terms", "current_version": "1.0"}]}))
    out = run(g.outstanding(subject="u1", tenant="t1"))
    assert out == [{"doc_id": "terms", "current_version": "1.0"}]


def test_empty_owed_is_nothing_owed():
    g = _gate()
    g._httpx = _Httpx(_Resp(200, {"outstanding": []}))
    assert run(g.outstanding(subject="u1", tenant="t1")) == []


def test_bare_string_items_are_coerced_to_doc_id():
    g = _gate()
    g._httpx = _Httpx(_Resp(200, {"outstanding": ["terms", "privacy"]}))
    assert run(g.outstanding(subject="u1", tenant="t1")) == [{"doc_id": "terms"}, {"doc_id": "privacy"}]


def test_non_200_raises_never_reads_as_empty():
    g = _gate()
    g._httpx = _Httpx(_Resp(500, {"outstanding": []}))
    with pytest.raises(LegalGateUnavailable):
        run(g.outstanding(subject="u1", tenant="t1"))


def test_missing_field_raises():
    g = _gate()
    g._httpx = _Httpx(_Resp(200, {}))          # field absent → None → NOT a list
    with pytest.raises(LegalGateUnavailable):
        run(g.outstanding(subject="u1", tenant="t1"))


def test_malformed_body_raises():
    g = _gate()
    g._httpx = _Httpx(_Resp(200, {"outstanding": "not-a-list"}))
    with pytest.raises(LegalGateUnavailable):
        run(g.outstanding(subject="u1", tenant="t1"))


def test_network_error_raises_legal_gate_unavailable():
    g = _gate()
    g._httpx = _Httpx(exc=ConnectionError("dns"))
    with pytest.raises(LegalGateUnavailable):
        run(g.outstanding(subject="u1", tenant="t1"))


def test_bad_json_raises():
    g = _gate()
    g._httpx = _Httpx(_Resp(200, ValueError("bad json")))
    with pytest.raises(LegalGateUnavailable):
        run(g.outstanding(subject="u1", tenant="t1"))


def test_ttl_zero_never_caches():
    calls: list = []
    g = _gate(ttl=0)
    g._httpx = _Httpx(_Resp(200, {"outstanding": []}), counter=calls)
    run(g.outstanding(subject="u1", tenant="t1"))
    run(g.outstanding(subject="u1", tenant="t1"))
    assert len(calls) == 2                     # every call hits compliance — accept clears immediately


def test_positive_ttl_caches_within_window():
    calls: list = []
    g = _gate(ttl=60)
    g._httpx = _Httpx(_Resp(200, {"outstanding": [{"doc_id": "terms"}]}), counter=calls)
    a = run(g.outstanding(subject="u1", tenant="t1"))
    b = run(g.outstanding(subject="u1", tenant="t1"))
    assert a == b and len(calls) == 1          # second served from cache
