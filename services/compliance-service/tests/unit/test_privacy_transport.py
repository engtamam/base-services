"""PrivacyTransport adapters: the classification IS the safety property.

The one failure mode that matters is a transport reporting `acked` for work that did not happen —
that turns "her data was erased" into a lie the system tells itself and its regulator. So every test
here is really asking the same question: can this adapter ever say acked when it shouldn't?
"""
from __future__ import annotations

import asyncio
import io
import json
import urllib.error

import pytest

from app.adapters.privacy_transport.http import HttpPrivacyTransport
from app.adapters.privacy_transport.null import NullPrivacyTransport
from app.container import build_container
from app.config import Settings
from app.ports.privacy_transport import ACKED, FAILED, PENDING, PrivacyTransport

_ARGS = dict(url="http://identity:8080/internal/privacy/erase", token="t" * 40,
             request_id="dsar_1", action="erase", tenant="acme", subject="u_7", timeout=5)
_ACK = {"service": "identity-service", "category": "identity", "subject_ref": "u_7",
        "erased": ["profile"], "retained": [], "already_absent": False}


def run(c):
    return asyncio.run(c)


class _Resp(io.BytesIO):
    def __init__(self, status, body=b""):
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _urlopen(monkeypatch, resp):
    seen = {}

    def fake(req, timeout=None):
        seen["req"], seen["timeout"] = req, timeout
        if isinstance(resp, Exception):
            raise resp
        return resp

    monkeypatch.setattr("app.adapters.privacy_transport.http.urllib.request.urlopen", fake)
    return seen


def test_both_adapters_satisfy_the_port():
    assert isinstance(NullPrivacyTransport(), PrivacyTransport)
    assert isinstance(HttpPrivacyTransport(), PrivacyTransport)


def test_null_never_fakes_success():
    t = NullPrivacyTransport()
    out = run(t.erase(**_ARGS))
    assert out.status == FAILED and not out.is_acked
    assert "nothing was dispatched" in out.detail
    assert len(t.calls) == 1          # it records, so a test can prove it was reached


def test_http_200_with_ack_body_is_acked_and_carries_the_evidence(monkeypatch):
    seen = _urlopen(monkeypatch, _Resp(200, json.dumps(_ACK).encode()))
    out = run(HttpPrivacyTransport().erase(**_ARGS))
    assert out.status == ACKED and out.ack == _ACK
    req = seen["req"]
    assert req.method == "POST" and req.full_url == _ARGS["url"]
    assert req.get_header("X-internal-token") == _ARGS["token"]
    assert json.loads(req.data) == {"tenant": "acme", "subject": "u_7",
                                    "action": "erase", "request_id": "dsar_1"}
    assert seen["timeout"] == 5       # the configured budget is actually applied


def test_http_202_is_pending_not_done(monkeypatch):
    _urlopen(monkeypatch, _Resp(202))
    out = run(HttpPrivacyTransport().erase(**_ARGS))
    assert out.status == PENDING and out.ack is None


@pytest.mark.parametrize("body", [b"not json", b"[1,2]", b'"a string"'])
def test_http_200_without_a_readable_ack_object_is_failed(monkeypatch, body):
    # A target that cannot say WHAT it erased has not answered the question we asked.
    _urlopen(monkeypatch, _Resp(200, body))
    out = run(HttpPrivacyTransport().erase(**_ARGS))
    assert out.status == FAILED


@pytest.mark.parametrize("boom", [
    urllib.error.HTTPError("u", 500, "boom", {}, None),
    urllib.error.HTTPError("u", 403, "nope", {}, None),
    urllib.error.URLError("connection refused"),
    OSError("timed out"),
])
def test_http_errors_are_failed_never_raised(monkeypatch, boom):
    # It must not raise: the dispatcher needs an outcome it can record and retry.
    _urlopen(monkeypatch, boom)
    out = run(HttpPrivacyTransport().erase(**_ARGS))
    assert out.status == FAILED and out.detail


@pytest.mark.parametrize("driver", ["null", "http"])
def test_container_wires_the_configured_driver(driver):
    c = build_container(Settings(INTERNAL_SERVICE_TOKEN="x" * 40,
                                 PRIVACY_TRANSPORT_DRIVER=driver))
    assert c.privacy_transport.driver == driver
    assert {"port": "privacy_transport", "driver": driver} in c.describe()


def test_unknown_driver_fails_at_boot():
    from app.ports.errors import ConfigError
    with pytest.raises(ConfigError, match="privacy_transport"):
        build_container(Settings(INTERNAL_SERVICE_TOKEN="x" * 40,
                                 PRIVACY_TRANSPORT_DRIVER="carrier-pigeon"))
