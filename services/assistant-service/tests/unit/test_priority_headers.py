"""The Vertex priority tier is double-locked infrastructure: it exists but is inert by default.

Priority headers ride out ONLY when the platform lock (ASSISTANT_PRIORITY_TIER_ENABLED) is on AND
the caller set priority=true AND the connection is Vertex. Any other combination is byte-identical to
the standard tier — nobody incurs the ~1.8x surcharge by accident.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

from app.config import get_settings
from app.api.internal import _priority_headers

_H_TYPE = "X-Vertex-AI-LLM-Request-Type"
_H_SHARED = "X-Vertex-AI-LLM-Shared-Request-Type"


def _req():
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))


def _conn(auth="vertex"):
    return SimpleNamespace(auth=auth)


def _with_lock(value: str):
    saved = dict(os.environ)
    os.environ["ASSISTANT_PRIORITY_TIER_ENABLED"] = value
    get_settings.cache_clear()
    return saved


def _restore(saved):
    os.environ.clear(); os.environ.update(saved); get_settings.cache_clear()


def test_both_locks_open_on_vertex_sends_priority_headers():
    saved = _with_lock("true")
    try:
        h = _priority_headers(_req(), _conn("vertex"), True)
        assert h == {_H_TYPE: "shared", _H_SHARED: "priority"}
    finally:
        _restore(saved)


def test_platform_lock_off_sends_nothing_even_when_requested():
    saved = _with_lock("false")
    try:
        assert _priority_headers(_req(), _conn("vertex"), True) == {}
    finally:
        _restore(saved)


def test_call_flag_off_sends_nothing_even_when_platform_enabled():
    saved = _with_lock("true")
    try:
        assert _priority_headers(_req(), _conn("vertex"), False) == {}
    finally:
        _restore(saved)


def test_non_vertex_connection_never_gets_the_header():
    saved = _with_lock("true")
    try:
        assert _priority_headers(_req(), _conn("api_key"), True) == {}
    finally:
        _restore(saved)
