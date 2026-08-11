"""Internal-tier provisioning: a product's signup wires the data map and fetches a DSAR verify code,
without holding compliance.manage (which would also let it DELETE targets — a worse PDPL posture)."""
from __future__ import annotations

from starlette.testclient import TestClient

from app.config import get_settings
from app.main import create_app


def _c():
    return TestClient(create_app())


def _tok():
    return {"X-Internal-Token": get_settings().INTERNAL_SERVICE_TOKEN}


def test_register_target_requires_the_internal_token():
    r = _c().post("/internal/compliance/data-targets", json={"service": "s", "category": "c"})
    assert r.status_code == 401


def test_register_target_wires_the_map_for_a_tenant():
    c = _c()
    r = c.post("/internal/compliance/data-targets", headers=_tok(),
               json={"tenant": "acme", "service": "file-service", "category": "files"})
    assert r.status_code == 200 and r.json()["tenant"] == "acme"
    # now the tenant's map is non-empty — the thing DsarService.dispatch needs to not refuse
    lst = c.get("/internal/compliance/held-refs", headers=_tok(), params={"tenant": "acme"})  # sanity: internal reachable
    assert lst.status_code in (200, 404)


def test_verification_code_404_for_unknown_dsar():
    assert _c().get("/internal/compliance/dsar/nope/verification-code", headers=_tok()).status_code == 404


def test_verification_code_requires_the_internal_token():
    assert _c().get("/internal/compliance/dsar/x/verification-code").status_code == 401
