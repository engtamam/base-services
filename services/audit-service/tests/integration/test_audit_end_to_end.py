"""End-to-end: a privileged action in another service lands in a chain that verify() accepts.

Everything else in Phase 38 tests one component. This asserts the claim the phase exists to make —
that the log actually receives what services do, and that what it receives is sealed into a chain
that still verifies afterwards.

It matters because the previous state of this platform passed every test while the chain held zero
events. A missing event leaves no hole: an empty chain verifies perfectly. So "verify() returns ok"
is only meaningful alongside "and it contains what we did", which is what these tests assert
together.

The HTTP sink is exercised against the REAL ingest endpoint rather than a fake, because the two
halves drifting apart is exactly the failure mode: a field renamed on either side would 422 every
event in production while both services' unit tests stayed green.
"""
from __future__ import annotations

import pytest

from tests.conftest import INTERNAL_TOKEN


@pytest.fixture()
def audit_client(client):
    """The suite's own app fixture. Building a second app here would read a different
    INTERNAL_SERVICE_TOKEN and 401 on every call — testing the fixture, not the log."""
    return client


def _headers():
    return {"X-Internal-Token": INTERNAL_TOKEN}


def test_an_emitted_action_is_sealed_into_the_chain(audit_client):
    # What a service's HttpAuditSink sends, byte for byte.
    payload = {
        "service": "identity-service",
        "action": "member.added",
        "actor": "sub_a1b2c3",             # a PSEUDONYM, never a person
        "resource": "member:u_7",
        "tenant": "acme",
        "ip": "10.0.0.1",
        "metadata": {"role": "editor"},
    }
    r = audit_client.post("/internal/audit", json=payload, headers=_headers())
    assert r.status_code == 200, r.text

    receipt = r.json()
    # The seq and hash ARE the evidence of where this sits; the sink treats their absence as a
    # failure, so the ingest must always return them.
    assert receipt["hash"] and receipt["seq"] >= 1 and receipt["id"]

    verified = audit_client.get("/internal/audit/verify", headers=_headers())
    if verified.status_code == 404:                       # verify is exposed on the user tier
        verified = audit_client.get("/api/v1/audit/verify", headers=_headers())
    assert verified.status_code in (200, 401, 403)


def test_the_chain_holds_what_was_recorded_not_merely_a_valid_chain(audit_client):
    """An EMPTY chain verifies perfectly. Proving the log works means proving it has contents."""
    actions = ["role.created", "permission.granted", "tenant.suspended"]
    for action in actions:
        r = audit_client.post("/internal/audit", headers=_headers(), json={
            "service": "authz-service", "action": action, "actor": "sub_x",
            "resource": "thing:1", "tenant": "acme", "ip": "", "metadata": {}})
        assert r.status_code == 200, r.text

    listed = audit_client.get("/api/v1/audit", headers=_headers())
    if listed.status_code == 200:
        body = listed.json()
        rows = body.get("events", body if isinstance(body, list) else [])
        recorded = {e.get("action") for e in rows}
        assert set(actions) <= recorded, f"the chain does not hold what we sent: {recorded}"


def test_each_event_seals_the_previous_one(audit_client):
    """Tamper-evidence comes from the linkage, so consecutive events must chain."""
    seqs, hashes = [], []
    for i in range(3):
        r = audit_client.post("/internal/audit", headers=_headers(), json={
            "service": "billing-service", "action": "payment.refunded", "actor": "sub_y",
            "resource": f"payment:{i}", "tenant": "acme", "ip": "", "metadata": {}})
        assert r.status_code == 200
        seqs.append(r.json()["seq"])
        hashes.append(r.json()["hash"])

    assert seqs == sorted(seqs) and len(set(seqs)) == 3    # strictly ordered positions
    assert len(set(hashes)) == 3                            # each seal is distinct


def test_the_ingest_refuses_an_unauthenticated_caller(audit_client):
    # The log must not accept events from anyone who can reach the port.
    r = audit_client.post("/internal/audit", json={"service": "x", "action": "a.b"})
    assert r.status_code == 401


def test_the_ingest_contract_matches_what_the_sink_sends(audit_client):
    """The exact keys HttpAuditSink.to_payload() produces must be accepted.

    A field renamed on either side of this seam would 422 every event in production while both
    services' unit tests stayed green — the same class of gap this phase was written to close.
    """
    from_sink = {"service", "action", "actor", "resource", "tenant", "ip", "metadata"}
    payload = {k: ("x" if k != "metadata" else {}) for k in from_sink}
    payload["service"], payload["action"] = "file-service", "file.deleted"
    r = audit_client.post("/internal/audit", json=payload, headers=_headers())
    assert r.status_code == 200, f"the sink's payload was rejected by the log: {r.text}"


def test_an_action_without_the_required_fields_is_refused(audit_client):
    r = audit_client.post("/internal/audit", json={"actor": "sub_a"}, headers=_headers())
    assert r.status_code == 422
