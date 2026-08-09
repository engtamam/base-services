"""Machine identity — the client-credentials grant end to end.

Proves the security properties the feature exists for: a tenant admin mints a machine credential
scoped to a SUBSET of its own capabilities; the secret is shown once; the exchange returns an
ordinary access JWT whose subject is a visibly-machine "svc_…" with the account's caps and tenant;
a wrong secret, a revoked account, and an unknown client are one 401; rotation invalidates the old
secret; and the whole surface 404s when the feature is off.
"""
from __future__ import annotations

import datetime as dt
import os

import jwt
import pytest
from fastapi.testclient import TestClient

TOKEN_SECRET = "h" * 40
ISS, AUD = "iss", "aud"


def _make_app(enabled: bool = True, **env_over) -> TestClient:
    from app.config import get_settings
    env = {
        "ENV": "test", "INTERNAL_SERVICE_TOKEN": "t" * 40,
        "TOKEN_VERIFIER_DRIVER": "hs256", "TOKEN_HS256_SECRET": TOKEN_SECRET,
        "TOKEN_ISSUER": ISS, "TOKEN_AUDIENCE": AUD, "RATELIMIT_MAX": "1000",
        "TOKEN_ISSUER_DRIVER": "hs256", "TOKEN_SIGNING_SECRET": TOKEN_SECRET,
        "PII_SALT": "s" * 32,
        "IDENTITY_MULTI_TENANT": "true",
        "SERVICE_ACCOUNTS_ENABLED": "true" if enabled else "false",
    }
    env.update(env_over)
    os.environ.update(env)
    get_settings.cache_clear()
    from app.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


def _admin(*caps: str, tenant: str = "acme") -> dict:
    """A human admin token carrying `caps` (and its tenant) — the issuer of service accounts."""
    now = dt.datetime.now(dt.timezone.utc)
    tok = jwt.encode({"sub": "admin1", "iss": ISS, "aud": AUD, "caps": list(caps),
                      "tenant": tenant, "type": "access",
                      "exp": now + dt.timedelta(minutes=5), "iat": now},
                     TOKEN_SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {tok}"}


def _decode(access: str) -> dict:
    return jwt.decode(access, TOKEN_SECRET, algorithms=["HS256"], audience=AUD, issuer=ISS)


@pytest.fixture
def client():
    from app.config import get_settings
    saved = dict(os.environ)
    c = _make_app()
    yield c
    os.environ.clear(); os.environ.update(saved); get_settings.cache_clear()


WRITE = "service_accounts:write"
READ = "service_accounts:read"


# ── issue ────────────────────────────────────────────────────────────────────────
def test_issue_returns_client_id_and_secret_once(client):
    r = client.post("/api/v1/service-accounts", headers=_admin(WRITE, "wallet:read"),
                    json={"name": "Odoo", "caps": ["wallet:read"]})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["client_id"].startswith("svc_")
    assert body["client_secret"]          # shown once
    assert body["caps"] == ["wallet:read"]
    assert body["tenant"] == "acme"
    assert body["grant_type"] == "client_credentials"

    # The secret appears in NO other route — a list never returns it.
    lst = client.get("/api/v1/service-accounts", headers=_admin(READ)).json()["service_accounts"]
    assert any(a["client_id"] == body["client_id"] for a in lst)
    assert all("client_secret" not in a and "secret_hash" not in a for a in lst)


def test_issuing_requires_the_write_capability(client):
    # A caller without the manage cap is refused (deny-by-default).
    assert client.post("/api/v1/service-accounts", headers=_admin("wallet:read"),
                       json={"name": "x", "caps": []}).status_code == 403


def test_cannot_grant_a_capability_the_issuer_does_not_hold(client):
    # Least privilege / no escalation (CWE-269): the issuer holds WRITE but not wallet:write.
    r = client.post("/api/v1/service-accounts", headers=_admin(WRITE),
                    json={"name": "x", "caps": ["wallet:write"]})
    # 403 — the caller PASSED the manage-cap gate (it holds WRITE), so the refusal is the escalation
    # guard. The error middleware generalises the body, so the status is the assertion.
    assert r.status_code == 403


def test_platform_caps_are_stripped_from_a_tenant_credential(client):
    # A platform-capable admin on a NON-platform tenant cannot embed platform.* / * in a machine
    # credential — the same last door human tokens pass.
    # The admin holds the manage cap AND the "*" wildcard (identity's require_cap matches literally,
    # so "*" alone would not pass the gate — the gateway is where "*" is honoured).
    r = client.post("/api/v1/service-accounts", headers=_admin(WRITE, "*", tenant="acme"),
                    json={"name": "x", "caps": ["*", "platform.admin", "wallet:read"]})
    assert r.status_code == 201, r.text
    assert r.json()["caps"] == ["wallet:read"]


# ── token exchange ────────────────────────────────────────────────────────────────
def _issue(client, caps=("wallet:read",)):
    r = client.post("/api/v1/service-accounts", headers=_admin(WRITE, *caps),
                    json={"name": "Odoo", "caps": list(caps)})
    assert r.status_code == 201, r.text
    return r.json()["client_id"], r.json()["client_secret"]


def test_client_credentials_exchange_returns_a_service_token(client):
    cid, secret = _issue(client)
    r = client.post("/api/v1/oauth/token",
                    json={"grant_type": "client_credentials", "client_id": cid,
                          "client_secret": secret})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_type"] == "Bearer"
    assert "refresh_token" not in body           # machines re-exchange, never refresh
    claims = _decode(body["access_token"])
    # An ordinary access JWT: sub is the visibly-machine client_id, with the account's caps + tenant.
    assert claims["sub"] == cid and cid.startswith("svc_")
    assert claims["caps"] == ["wallet:read"]
    assert claims["tenant"] == "acme"
    assert claims["actor_type"] == "service"
    assert claims["type"] == "access"


def test_a_wrong_secret_is_invalid_client(client):
    cid, _ = _issue(client)
    r = client.post("/api/v1/oauth/token",
                    json={"grant_type": "client_credentials", "client_id": cid,
                          "client_secret": "wrong"})
    assert r.status_code == 401


def test_an_unknown_client_is_invalid_client(client):
    r = client.post("/api/v1/oauth/token",
                    json={"grant_type": "client_credentials", "client_id": "svc_nope",
                          "client_secret": "whatever"})
    assert r.status_code == 401


def test_an_unsupported_grant_type_is_refused(client):
    cid, secret = _issue(client)
    r = client.post("/api/v1/oauth/token",
                    json={"grant_type": "password", "client_id": cid, "client_secret": secret})
    assert r.status_code == 400


# ── revoke / rotate ────────────────────────────────────────────────────────────────
def test_revoke_stops_the_token_exchange(client):
    cid, secret = _issue(client)
    assert client.delete(f"/api/v1/service-accounts/{cid}",
                         headers=_admin(WRITE)).status_code == 200
    r = client.post("/api/v1/oauth/token",
                    json={"grant_type": "client_credentials", "client_id": cid,
                          "client_secret": secret})
    assert r.status_code == 401           # a revoked account authenticates nothing


def test_rotate_with_overlap_keeps_the_old_secret_working_until_it_expires(client):
    # The fleet-migration case: a POS build on hundreds of tills cannot all update at the instant
    # rotate is pressed. The OLD secret must keep working during the overlap window, then stop ON ITS
    # OWN. This is the reporter's exact assertion: rotate; old still authenticates; advance past the
    # window; old no longer does — with no manual step in between.
    cid, old = _issue(client)
    r = client.post(f"/api/v1/service-accounts/{cid}/rotate",
                    headers=_admin(WRITE), json={"overlap_seconds": 3600})
    new = r.json()["client_secret"]
    assert new != old and r.json()["previous_secret_expires_at"] > 0
    body = {"grant_type": "client_credentials", "client_id": cid}
    # BOTH secrets authenticate inside the window.
    assert client.post("/api/v1/oauth/token", json={**body, "client_secret": old}).status_code == 200
    assert client.post("/api/v1/oauth/token", json={**body, "client_secret": new}).status_code == 200

    # Advance past the window (move the module clock forward) — the OLD secret lapses by timestamp.
    from app.core import service_accounts as sa
    real_now = sa._now
    try:
        sa._now = lambda: real_now() + 3601
        assert client.post("/api/v1/oauth/token", json={**body, "client_secret": old}).status_code == 401
        assert client.post("/api/v1/oauth/token", json={**body, "client_secret": new}).status_code == 200
    finally:
        sa._now = real_now


def test_rotate_with_zero_overlap_is_an_immediate_cutover_for_a_leak(client):
    cid, old = _issue(client)
    new = client.post(f"/api/v1/service-accounts/{cid}/rotate",
                      headers=_admin(WRITE), json={"overlap_seconds": 0}).json()["client_secret"]
    body = {"grant_type": "client_credentials", "client_id": cid}
    # No window: the old secret dies at once — the right response to a leak.
    assert client.post("/api/v1/oauth/token", json={**body, "client_secret": old}).status_code == 401
    assert client.post("/api/v1/oauth/token", json={**body, "client_secret": new}).status_code == 200


def test_a_second_rotation_inside_a_window_drops_the_oldest_secret(client):
    # Two live overlaps is a credential nobody can reason about. After a second rotation, only the
    # single most-recent previous survives; the original is gone even though its window had not lapsed.
    cid, s1 = _issue(client)
    s2 = client.post(f"/api/v1/service-accounts/{cid}/rotate",
                     headers=_admin(WRITE), json={"overlap_seconds": 3600}).json()["client_secret"]
    s3 = client.post(f"/api/v1/service-accounts/{cid}/rotate",
                     headers=_admin(WRITE), json={"overlap_seconds": 3600}).json()["client_secret"]
    body = {"grant_type": "client_credentials", "client_id": cid}
    assert client.post("/api/v1/oauth/token", json={**body, "client_secret": s3}).status_code == 200  # current
    assert client.post("/api/v1/oauth/token", json={**body, "client_secret": s2}).status_code == 200  # the one prev
    assert client.post("/api/v1/oauth/token", json={**body, "client_secret": s1}).status_code == 401  # dropped


def test_one_tenant_cannot_touch_anothers_service_account(client):
    cid, _ = _issue(client)                # created under tenant "acme"
    # A WRITE-capable admin of a DIFFERENT tenant sees a 404, not the account.
    assert client.delete(f"/api/v1/service-accounts/{cid}",
                         headers=_admin(WRITE, tenant="rival")).status_code == 404


# ── feature flag ────────────────────────────────────────────────────────────────────
def test_the_surface_404s_when_disabled():
    from app.config import get_settings
    saved = dict(os.environ)
    c = _make_app(enabled=False)
    try:
        assert c.post("/api/v1/oauth/token",
                      json={"grant_type": "client_credentials"}).status_code == 404
        assert c.post("/api/v1/service-accounts", headers=_admin(WRITE),
                      json={"name": "x", "caps": []}).status_code == 404
    finally:
        os.environ.clear(); os.environ.update(saved); get_settings.cache_clear()
