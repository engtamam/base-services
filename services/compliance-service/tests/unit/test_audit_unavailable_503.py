"""A refused critical action must answer 503, not 500.

_template/app/auditable_actions.py has always said "the API layer turns it into a 503", and
AuditUnavailable's own docstring says the same. Neither was true: no service registered a handler,
so the exception fell to the catch-all `Exception` handler and every refusal surfaced as
500 INTERNAL_ERROR.

The difference is not cosmetic. 500 says "we crashed" and invites a bug report; 503 + Retry-After
says "this action needs an audit record and the log is unreachable, try again" and invites a retry.
The fail-closed behaviour was always correct — the mutation is skipped and nothing commits. It was
the DIAGNOSIS that was wrong.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.errors import register_error_handlers
from app.core.audit import AuditUnavailable


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "x" * 40)
    app = FastAPI()
    register_error_handlers(app)

    @app.post("/refused")
    async def _refused():
        raise AuditUnavailable(
            "this action requires an audit record and the audit log is unavailable")

    @app.post("/boom")
    async def _boom():
        raise RuntimeError("something else entirely")

    # raise_server_exceptions=False so the handler runs instead of the exception escaping the
    # test client — otherwise this would pass by never exercising the thing under test.
    return TestClient(app, raise_server_exceptions=False)


def test_a_refused_critical_action_is_503_not_500(client):
    """THE regression. It was 500 before the handler existed."""
    r = client.post("/refused")
    assert r.status_code == 503
    assert r.json()["error"] == "AUDIT_UNAVAILABLE"


def test_retry_after_is_present_and_numeric(client):
    """Without it a client has no basis for backoff and will hammer a service whose audit log is
    already struggling."""
    r = client.post("/refused")
    assert "Retry-After" in r.headers
    assert int(r.headers["Retry-After"]) > 0


def test_the_body_does_not_name_the_action(client):
    """That the log is down is operational. WHICH privileged action someone attempted is not
    something an error body should reveal — the exception text is deliberately generic and the
    handler must not add detail back."""
    body = client.post("/refused").text.lower()
    for leak in ("legal_hold", "release", "erase", "purge", "dsar"):
        assert leak not in body


def test_an_unrelated_error_is_still_500(client):
    """The new handler must be SPECIFIC. If it widened to catch everything, a genuine crash would
    start advertising itself as retryable and clients would retry a bug forever."""
    r = client.post("/boom")
    assert r.status_code == 500
    assert r.json()["error"] == "INTERNAL_ERROR"
    assert "Retry-After" not in r.headers
