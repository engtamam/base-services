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
from app.ports.privacy_transport import ACKED, FAILED, PARTIAL, PENDING, PrivacyTransport

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


def test_a_truncated_ack_is_partial_not_acked(monkeypatch):
    # Item 2: a target that erased only PART of the rows (truncated) must NOT be recorded as a full
    # acknowledgement — it is PARTIAL, stays unacked, and re-dispatches.
    ack = {**_ACK, "erased": ["rows:5000"], "truncated": True}
    _urlopen(monkeypatch, _Resp(200, json.dumps(ack).encode()))
    out = run(HttpPrivacyTransport().erase(**_ARGS))
    assert out.status == PARTIAL and out.is_acked is False and out.is_partial is True
    assert out.ack == ack                              # the partial evidence is kept


def test_an_empty_subject_is_refused_before_any_call(monkeypatch):
    # Item 4: an empty subject would erase against nothing and come back already_absent — a clean ack
    # over no work. Refused as a distinguishable FAILED, and Meta is never called.
    called = {"n": 0}
    monkeypatch.setattr("app.adapters.privacy_transport.http.urllib.request.urlopen",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    out = run(HttpPrivacyTransport().erase(**{**_ARGS, "subject": "   "}))
    assert out.status == FAILED and "INVALID_SUBJECT" in out.detail and called["n"] == 0


def test_a_zero_row_ack_is_acked_but_distinguishable(monkeypatch):
    # Item 4: already_absent (held nothing) is a legitimate ack, but recorded DISTINCTLY — if every
    # target says it, the subject id was probably wrong (the double-hash bug).
    ack = {**_ACK, "erased": [], "already_absent": True}
    _urlopen(monkeypatch, _Resp(200, json.dumps(ack).encode()))
    out = run(HttpPrivacyTransport().erase(**_ARGS))
    assert out.status == ACKED and "already_absent" in out.detail


def test_unrecognized_subject_kind_is_failed_not_acked(monkeypatch):
    # Item 6: a target that does not index by the KIND of identifier we sent (it holds internal ids,
    # we sent a phone hash) refuses by returning unrecognized_subject_kind=true. Even on a 200 that
    # must be FAILED, never ACKED — matching zero rows and closing the DSAR is a false "erased". The
    # DSAR-protecting classification lives HERE, so any target in any language is safe by setting it.
    ack = {**_ACK, "erased": [], "unrecognized_subject_kind": True}
    _urlopen(monkeypatch, _Resp(200, json.dumps(ack).encode()))
    out = run(HttpPrivacyTransport().erase(**{**_ARGS, "subject_kind": "phone_hash"}))
    assert out.status == FAILED and out.is_acked is False
    assert "unrecognized_subject_kind" in out.detail and "phone_hash" in out.detail
    assert out.ack == ack                              # the refusal evidence is kept


def test_blocked_by_legal_hold_is_failed_not_acked(monkeypatch):
    # r2-02: a target that lawfully RETAINED the subject's data under its own legal hold returns 200
    # with blocked_by_legal_hold=true and erased=[]. That is a truthful ack but NOT an erasure — it
    # must be FAILED (never ACKED), or the target flips to "done", the DSAR is derived COMPLETED, and
    # the regulator is told the person was erased while the data is deliberately kept.
    ack = {**_ACK, "erased": [], "already_absent": False, "blocked_by_legal_hold": True}
    _urlopen(monkeypatch, _Resp(200, json.dumps(ack).encode()))
    out = run(HttpPrivacyTransport().erase(**_ARGS))
    assert out.status == FAILED and out.is_acked is False
    assert "blocked_by_legal_hold" in out.detail
    assert out.ack == ack                              # the retention evidence is kept


def test_subject_kind_travels_in_the_payload_only_when_set(monkeypatch):
    # Item 6: subject_kind is additive — sent when known so a target can refuse, and OMITTED when not,
    # so an existing target sees the exact same request body as before (additionalProperties-safe).
    seen = _urlopen(monkeypatch, _Resp(200, json.dumps(_ACK).encode()))
    run(HttpPrivacyTransport().erase(**{**_ARGS, "subject_kind": "internal_id"}))
    assert json.loads(seen["req"].data)["subject_kind"] == "internal_id"
    seen2 = _urlopen(monkeypatch, _Resp(200, json.dumps(_ACK).encode()))
    run(HttpPrivacyTransport().erase(**_ARGS))         # no subject_kind
    assert "subject_kind" not in json.loads(seen2["req"].data)


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
