"""Structured logging redacts secrets (CWE-532)."""
from __future__ import annotations

import json
import logging

from app.observability.logging import JsonFormatter


def _format(**extra) -> dict:
    fmt = JsonFormatter(service="svc", env="production")
    record = logging.makeLogRecord({"msg": "event", "levelname": "INFO", "name": "t"})
    for k, v in extra.items():
        setattr(record, k, v)
    return json.loads(fmt.format(record))


def test_redacts_sensitive_fields():
    out = _format(authorization="Bearer secret123", password="hunter2", token="abc")
    assert out["authorization"] == "[REDACTED]"
    assert out["password"] == "[REDACTED]"
    assert out["token"] == "[REDACTED]"


def test_redacts_nested_dict():
    out = _format(context={"api_key": "k", "user": "alice"})
    assert out["context"]["api_key"] == "[REDACTED]"
    assert out["context"]["user"] == "alice"


def test_keeps_safe_fields_and_shape():
    out = _format(request_id="r1", status=200)
    assert out["request_id"] == "r1"
    assert out["status"] == 200
    assert out["service"] == "svc"
    assert out["msg"] == "event"
    assert out["level"] == "info"
