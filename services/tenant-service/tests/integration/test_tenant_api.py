"""Integration: platform admin + tenant-self + internal resolve."""
from __future__ import annotations

import datetime as dt
import os

import jwt
import pytest

from fastapi.testclient import TestClient

SECRET = "h" * 40
ITOKEN = "i" * 40
BASE = "apps.example.com"


def _make_app(**over) -> TestClient:
    from app.config import get_settings

    env = {
        "ENV": "test", "INTERNAL_SERVICE_TOKEN": ITOKEN,
        "TOKEN_VERIFIER_DRIVER": "hs256", "TOKEN_HS256_SECRET": SECRET,
        "RATELIMIT_MAX": "1000", "BASE_DOMAIN": BASE,
    }
    env.update(over)
    os.environ.update(env)
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


def _tok(sub="u", caps=None, tenant=None):
    now = dt.datetime.now(dt.timezone.utc)
    claims = {"sub": sub, "exp": now + dt.timedelta(minutes=5), "iat": now}
    if caps is not None:
        claims["caps"] = caps
    if tenant:
        claims["tenant"] = tenant
    return jwt.encode(claims, SECRET, algorithm="HS256")


def _platform():
    return {"Authorization": "Bearer " + _tok("admin", ["platform.tenants"])}


def _internal():
    return {"X-Internal-Token": ITOKEN}


def test_provision_requires_platform_cap(client):
    # tenant.manage is NOT platform.tenants -> 403
    r = client.post("/api/v1/tenants", json={"slug": "acme", "name": "Acme"},
                    headers={"Authorization": "Bearer " + _tok("u", ["tenant.manage"])})
    assert r.status_code == 403


def test_provision_get_resolve(client):
    r = client.post("/api/v1/tenants",
                    json={"slug": "acme", "name": "Acme", "domains": ["acme.com"]},
                    headers=_platform())
    assert r.status_code == 201
    tid = r.json()["id"]

    got = client.get(f"/api/v1/tenants/{tid}", headers=_platform())
    assert got.status_code == 200 and got.json()["slug"] == "acme"

    # internal resolve by subdomain
    res = client.get(f"/internal/tenants/resolve?host=acme.{BASE}", headers=_internal())
    assert res.status_code == 200 and res.json()["slug"] == "acme"
    assert res.json()["isolation"]["mode"] == "shared-row"

    # internal resolve by mapped custom domain requires custom-domain resolver; subdomain
    # resolver returns 404 for the raw custom domain (expected here).
    assert client.get(f"/internal/tenants/resolve?host=acme.com", headers=_internal()).status_code == 404


def test_lifecycle_and_export(client):
    tid = client.post("/api/v1/tenants", json={"slug": "beta", "name": "Beta"},
                      headers=_platform()).json()["id"]
    assert client.post(f"/api/v1/tenants/{tid}/suspend", headers=_platform()).json()["status"] == "suspended"
    assert client.post(f"/api/v1/tenants/{tid}/reactivate", headers=_platform()).json()["status"] == "active"
    exp = client.get(f"/api/v1/tenants/{tid}/export", headers=_platform())
    assert exp.status_code == 200 and exp.json()["tenant"]["slug"] == "beta"
    # A delete leaves a TOMBSTONE, not a hole. The row is not purged, because `tenant.deleted` is
    # published at most once and a peer that missed it must still be able to LEARN the tenant is
    # gone — by reading. (This assertion used to demand `purged is True` and a 404, which is the
    # behaviour the tombstone exists to remove.)
    deleted = client.delete(f"/api/v1/tenants/{tid}", headers=_platform()).json()
    assert deleted["purged"] is False
    assert deleted["status"] == "deleted" and deleted["deleted_at"]

    # An operator can still see WHAT was deleted and WHEN — a delete that leaves no record is not
    # an audit trail, it is an amnesia.
    admin = client.get(f"/api/v1/tenants/{tid}", headers=_platform())
    assert admin.status_code == 200 and admin.json()["status"] == "deleted"
    # And a PEER can read the tombstone and learn the truth, which is the whole point.
    peer = client.get(f"/internal/tenants/{tid}", headers=_internal())
    assert peer.status_code == 200
    assert peer.json()["status"] == "deleted"


def test_tenant_self_scoped(client):
    tid = client.post("/api/v1/tenants", json={"slug": "gamma", "name": "Gamma"},
                      headers=_platform()).json()["id"]
    own = {"Authorization": "Bearer " + _tok("owner", ["tenant.manage"], tenant=tid)}
    me = client.get("/api/v1/tenant", headers=own)
    assert me.status_code == 200 and me.json()["slug"] == "gamma"
    # tenant-admin cannot use the platform cross-tenant list
    assert client.get("/api/v1/tenants", headers=own).status_code == 403


def test_duplicate_slug_conflict(client):
    client.post("/api/v1/tenants", json={"slug": "dup", "name": "A"}, headers=_platform())
    r = client.post("/api/v1/tenants", json={"slug": "dup", "name": "B"}, headers=_platform())
    assert r.status_code == 409
