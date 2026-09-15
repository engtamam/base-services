"""The push-dispatch internal routes: register/list/remove a target and run a dispatch pass.

Security surface under test: internal-token only (never a user JWT), and the SSRF allowlist enforced
at register time over the wire (a non-allow-listed host is a 400, not a stored target).
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

INTERNAL = "t" * 40
IT = {"X-Internal-Token": INTERNAL}


@pytest.fixture()
def client():
    from app.config import get_settings
    saved = dict(os.environ)
    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN=INTERNAL,
                      TOKEN_VERIFIER_DRIVER="hs256", TOKEN_HS256_SECRET="h" * 40,
                      RATELIMIT_MAX="100000",
                      PUSH_DISPATCH_ENABLED="true",
                      EGRESS_ALLOWED_HOSTS="peer-service")
    get_settings.cache_clear()
    try:
        from app.main import create_app
        with TestClient(create_app()) as c:
            yield c
    finally:
        os.environ.clear()
        os.environ.update(saved)
        get_settings.cache_clear()


def test_all_dispatch_routes_require_the_internal_token(client):
    assert client.post("/internal/dispatch/targets",
                       json={"queue": "q", "url": "http://peer-service/h"}).status_code == 401
    assert client.get("/internal/dispatch/targets").status_code == 401
    assert client.delete("/internal/dispatch/targets/q").status_code == 401
    assert client.post("/internal/dispatch/run").status_code == 401


def test_register_list_and_delete_a_target(client):
    r = client.post("/internal/dispatch/targets", headers=IT,
                    json={"queue": "notifications", "url": "http://peer-service/internal/jobs/run"})
    assert r.status_code == 201, r.text
    assert r.json()["queue"] == "notifications"

    lst = client.get("/internal/dispatch/targets", headers=IT).json()["targets"]
    assert any(t["queue"] == "notifications" for t in lst)

    d = client.delete("/internal/dispatch/targets/notifications", headers=IT)
    assert d.status_code == 200 and d.json()["removed"] is True
    # second delete is a clean false, not an error
    assert client.delete("/internal/dispatch/targets/notifications", headers=IT).json()["removed"] is False


def test_register_refuses_a_non_allowlisted_host_over_the_wire(client):
    r = client.post("/internal/dispatch/targets", headers=IT,
                    json={"queue": "q", "url": "http://attacker.example/steal"})
    assert r.status_code == 400
    # and nothing was stored
    assert client.get("/internal/dispatch/targets", headers=IT).json()["targets"] == []


def test_dispatch_run_is_callable_and_reports_counts(client):
    # No targets registered → a clean zero pass (enabled True from env).
    r = client.post("/internal/dispatch/run", headers=IT)
    assert r.status_code == 200
    assert r.json() == {"enabled": True, "dispatched": 0, "failed": 0}
