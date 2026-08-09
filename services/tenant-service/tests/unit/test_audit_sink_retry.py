"""The HTTP audit sink retries a transient failure SAFELY — same idempotency key, bounded attempts.

A single slow audit call used to fail the real user action it was witnessing (with AUDIT_FAIL_CLOSED
on a critical action, one timeout returned 503 to a shop-floor operator). The sink now retries, and
it is safe to do so because every emit carries an idempotency key the log dedups on. These tests pin
the behaviour that makes it safe: the key is STABLE across retries, only retryable failures retry,
and a 4xx never does.
"""
from __future__ import annotations

import asyncio
import io
import json
import socket
import urllib.error

import pytest

from app.adapters.audit_sink import http as sinkmod
from app.ports.audit_sink import ACCEPTED, FAILED, AuditEvent


class _Resp:
    def __init__(self, body):
        self._b = body
        self.status = 200

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _run(behaviors, monkeypatch, max_attempts=2):
    """behaviors: per-attempt exception, or None for success. Returns (receipt, request_bodies)."""
    bodies = []

    def fake_urlopen(req, timeout=None):
        bodies.append(json.loads(req.data))
        exc = behaviors[len(bodies) - 1]
        if exc is not None:
            raise exc
        return _Resp(json.dumps({"id": "e1", "seq": 1, "hash": "abc"}).encode())

    monkeypatch.setattr(sinkmod.urllib.request, "urlopen", fake_urlopen)
    sink = sinkmod.HttpAuditSink("http://audit/internal/audit", "tok", timeout=1,
                                 max_attempts=max_attempts, backoff=0)
    receipt = asyncio.run(sink.emit(AuditEvent(service="x", action="a")))
    return receipt, bodies


def test_a_timeout_is_retried_with_the_same_idempotency_key(monkeypatch):
    r, bodies = _run([socket.timeout("slow"), None], monkeypatch)
    assert r.status == ACCEPTED
    assert len(bodies) == 2
    assert bodies[0]["idempotency_key"] == bodies[1]["idempotency_key"]   # stable across the retry


def test_a_4xx_is_never_retried(monkeypatch):
    r, bodies = _run([urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(b"")), None], monkeypatch)
    assert r.status == FAILED and len(bodies) == 1          # one attempt, no retry


def test_a_503_is_retried(monkeypatch):
    r, bodies = _run([urllib.error.HTTPError("u", 503, "busy", {}, io.BytesIO(b"")), None], monkeypatch)
    assert r.status == ACCEPTED and len(bodies) == 2


def test_exhausting_the_attempts_reports_failed(monkeypatch):
    r, bodies = _run([socket.timeout("a"), socket.timeout("b")], monkeypatch)
    assert r.status == FAILED and len(bodies) == 2 and "2 attempt" in r.detail


def test_one_attempt_is_the_legacy_no_retry_behaviour(monkeypatch):
    r, bodies = _run([socket.timeout("a")], monkeypatch, max_attempts=1)
    assert r.status == FAILED and len(bodies) == 1
