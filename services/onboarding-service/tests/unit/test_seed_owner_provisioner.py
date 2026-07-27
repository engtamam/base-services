"""Contract: onboarding's generic HttpProvisioner speaks identity's seed-owner / revoke shapes.

The tenant_owner provisioner is pure deploy config (ONBOARDING_PROVISIONERS =
tenant_owner=<seed-owner-url>|<revoke-url>), so what needs proving is that the SHAPES agree: create
captures the `id` seed-owner returns as the undo reference, and undo DELETEs exactly that reference,
with 404 (already gone) treated as success — the orphan-owner compensation the saga relies on.
"""
from __future__ import annotations

import asyncio

from app.adapters.provisioner.http import HttpProvisioner
from app.ports.provisioner import CREATED, EXISTS, FAILED, ProvisionRequest


def run(c):
    return asyncio.run(c)


def _prov(monkeypatch, responses):
    p = HttpProvisioner("tenant_owner",
                        "http://identity/internal/tenancy/seed-owner",
                        "http://identity/internal/tenancy/membership/{id}", "tok")
    calls: list = []

    def fake_call(url, body, method="POST"):
        calls.append((method, url, body))
        for sub, (st, pl) in responses.items():
            if sub in url:
                return st, pl, None
        return 404, {}, None

    monkeypatch.setattr(p, "_call", fake_call)
    return p, calls


def test_create_captures_the_seed_owner_id_as_the_undo_reference(monkeypatch):
    p, calls = _prov(monkeypatch, {"seed-owner": (201, {"id": "acme::p_1", "person_id": "p_1",
                                                        "seeded": True})})
    req = ProvisionRequest(kind="tenant_owner", tenant="acme", subject="founder@acme", key="k1",
                           attributes={"identifier": "founder@acme", "caps": ["members:write"],
                                       "roles": ["owner"]})
    res = run(p.create(req))
    assert res.status == CREATED and res.reference == "acme::p_1"
    method, url, body = calls[0]
    assert method == "POST" and "seed-owner" in url
    # our attributes (identifier/caps/roles) + tenant + idempotency_key reach seed-owner
    assert body["tenant"] == "acme" and body["identifier"] == "founder@acme"
    assert body["caps"] == ["members:write"] and body["idempotency_key"] == "k1"


def test_undo_deletes_exactly_that_reference_and_404_is_success(monkeypatch):
    p, calls = _prov(monkeypatch, {"membership": (404, {})})
    res = run(p.undo(ProvisionRequest(kind="tenant_owner", tenant="acme", subject="f", key="k1"),
                     "acme::p_1"))
    assert res.status != FAILED                       # 404 (already gone) is a successful undo
    method, url, _ = calls[0]
    assert method == "DELETE" and url.endswith("/membership/acme::p_1")


def test_a_missing_id_in_the_response_is_a_failure_to_compensate_now(monkeypatch):
    # seed-owner that returned no id would leave an owner the saga could never undo — fail loudly.
    p, _ = _prov(monkeypatch, {"seed-owner": (201, {"person_id": "p_1"})})
    res = run(p.create(ProvisionRequest(kind="tenant_owner", tenant="acme", subject="f", key="k1")))
    assert res.status == FAILED
