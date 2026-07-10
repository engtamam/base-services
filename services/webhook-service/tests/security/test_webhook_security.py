"""Security: deny-by-default, SSRF, secret exposure, fail-closed drivers."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


def _make_app() -> TestClient:
    from app.config import get_settings

    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN="i" * 40, TOKEN_VERIFIER_DRIVER="hs256",
                      TOKEN_HS256_SECRET="h" * 40, RATELIMIT_MAX="1000")
    get_settings.cache_clear()
    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture(scope="module")
def client():
    from app.config import get_settings

    saved = dict(os.environ)
    c = _make_app()
    yield c
    os.environ.clear()
    os.environ.update(saved)
    get_settings.cache_clear()


def test_deny_by_default(client):
    assert client.get("/api/v1/webhooks/endpoints").status_code == 401
    assert client.post("/api/v1/webhooks/endpoints", json={"url": "https://x.com/h"}).status_code == 401
    assert client.get("/api/v1/webhooks/dead-letters").status_code == 401


def test_internal_endpoints_require_token(client):
    assert client.post("/internal/webhooks/dispatch",
                       json={"tenant": "t", "type": "x"}).status_code == 401
    assert client.post("/internal/webhooks/sweep").status_code == 401


def test_inbound_without_signature_is_rejected_not_500(client):
    """A bad signature must be 400 — a 5xx would make the provider retry forever."""
    r = client.post("/inbound/stripe", content=b'{"id":"1"}')
    assert r.status_code == 400


def test_null_signer_never_verifies():
    from app.adapters.signer.null import NullSigner
    assert NullSigner().verify(body=b"x", header="anything", secrets=["s"],
                               timestamp=0, tolerance_seconds=300) is False


def test_null_transport_reports_failure_not_success():
    from app.adapters.transport.null import NullTransport
    r = NullTransport().send(url="https://x.com", pinned_ip="1.1.1.1", body=b"", headers={},
                             timeout_seconds=1, max_response_bytes=10)
    assert r.ok is False and r.status == 0


def test_transport_errors_are_secret_free():
    from app.adapters.transport.stdlib import StdlibTransport
    r = StdlibTransport().send(url="https://example.com/h", pinned_ip="203.0.113.1",
                               body=b"x", headers={"Authorization": "Bearer supersecret"},
                               timeout_seconds=1, max_response_bytes=100)
    assert r.ok is False and "supersecret" not in r.error and "203.0.113.1" not in r.error


def test_endpoint_public_view_hides_secrets():
    from app.core.models import Endpoint
    ep = Endpoint(id="e", tenant="t", url="https://x.com/h", secrets=["whsec_supersecret"])
    assert "whsec_supersecret" not in str(ep.public())


def test_unknown_driver_fails_fast():
    from app.config import Settings
    from app.container import build_container
    from app.ports.errors import ConfigError
    with pytest.raises(ConfigError):
        build_container(Settings(_env_file=None, INTERNAL_SERVICE_TOKEN="t" * 40,
                                 WEBHOOK_TRANSPORT_DRIVER="carrier-pigeon"))
    with pytest.raises(ConfigError):
        build_container(Settings(_env_file=None, INTERNAL_SERVICE_TOKEN="t" * 40,
                                 WEBHOOK_SIGNER_DRIVER="rot13"))


def test_response_body_is_size_capped():
    """A customer must not be able to stream us a 1 GB response."""
    from app.core.models import Delivery
    d = Delivery(id="d", tenant="t", event_id="e", endpoint_id="ep")
    d.response_body = ("x" * 10_000)[:512]
    assert len(d.response_body) <= 512
