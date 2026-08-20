"""Scoped ticket sharing + KPI derivation — the two pure primitives behind cross-service visibility."""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.kpis import ticket_kpis
from app.core.share import ShareGrant, ShareStore, grant_id


def run(c):
    return asyncio.run(c)


# ── ShareGrant: consent that fails closed and scopes explicitly ─────────────────
def test_grant_id_is_one_per_tenant_grantee_and_length_prefixed():
    assert grant_id("acme", "mkr1") == "4:acme:mkr1"
    # a length prefix so no (tenant, grantee) pair can collide with another by moving the separator
    assert grant_id("acm", "e:mkr1") != grant_id("acme", "mkr1")


def test_grant_is_active_only_while_consent_stands():
    g = ShareGrant(tenant="acme", grantee="m", expires_at=1000.0)
    assert g.is_active(999.0) is True
    assert g.is_active(1000.0) is False               # lapsed -> grants nothing (fails closed)
    g.revoked = True
    assert g.is_active(0.0) is False                  # revoked -> grants nothing
    assert ShareGrant(tenant="acme", grantee="m").is_active(9e9) is True   # 0 = never expires


def test_scope_is_an_allowlist_never_a_denylist():
    g = ShareGrant(tenant="acme", grantee="m", scope={"type": ["support"], "status": ["open", "new"]})
    assert g.covers({"type": "support", "status": "new"}) is True
    assert g.covers({"type": "feedback", "status": "new"}) is False    # type not in the allowlist
    assert g.covers({"type": "support", "status": "closed"}) is False  # status not in the allowlist
    # an empty scope means ALL of the tenant's tickets
    assert ShareGrant(tenant="acme", grantee="m").covers({"type": "anything", "status": "x"}) is True


def test_share_store_upsert_get_revoke_is_idempotent():
    st = ShareStore(MemoryDataStore())
    run(st.upsert(ShareGrant(tenant="acme", grantee="m", granted_by="sub_x")))
    run(st.upsert(ShareGrant(tenant="acme", grantee="m", granted_by="sub_x")))   # upsert, not a 2nd row
    assert len(run(st.list_for_tenant("acme"))) == 1
    assert run(st.get("acme", "m")).granted_by == "sub_x"
    assert run(st.revoke("acme", "m")) is True
    assert run(st.revoke("acme", "m")) is False        # idempotent: already revoked
    assert run(st.get("acme", "m")).revoked is True     # kept as a tombstone, not deleted


# ── ticket_kpis: derived from the event log, timings not content ─────────────────
def _ev(kind, created, seq, **data):
    return {"kind": kind, "created": created, "seq": seq, "data": data, "actor_ref": "who",
            "body": "SECRET CONTENT"}


def test_kpis_derive_first_response_from_the_first_operator_reply():
    ticket = {"created": "2026-01-01T00:00:00+00:00", "status": "in_progress"}
    events = [
        _ev("created", "2026-01-01T00:00:00+00:00", 1),
        _ev("replied", "2026-01-01T00:30:00+00:00", 2, by="customer"),   # NOT a response
        _ev("assigned", "2026-01-01T00:45:00+00:00", 3, assignee="agent7"),
        _ev("replied", "2026-01-01T01:00:00+00:00", 4, by="operator"),   # the first real answer
        _ev("replied", "2026-01-01T02:00:00+00:00", 5, by="operator"),
    ]
    k = ticket_kpis(ticket, events)
    assert k["first_response_at"] == "2026-01-01T01:00:00+00:00"
    assert k["first_response_seconds"] == 3600.0        # one hour, derived from created -> first op reply
    assert k["assigned_at"] == "2026-01-01T00:45:00+00:00" and k["assignee_ref"] == "agent7"
    assert k["operator_reply_count"] == 2 and k["reply_count"] == 3


def test_kpis_derive_escalation_and_resolution():
    ticket = {"created": "2026-01-01T00:00:00+00:00", "status": "closed"}
    events = [
        _ev("created", "2026-01-01T00:00:00+00:00", 1),
        _ev("escalated", "2026-01-01T00:20:00+00:00", 2, level="tier2"),
        _ev("status_changed", "2026-01-01T01:00:00+00:00", 3, to="in_progress", resolved=False),
        _ev("status_changed", "2026-01-01T03:00:00+00:00", 4, to="closed", resolved=True),
        _ev("rated", "2026-01-01T04:00:00+00:00", 5, score=5),
    ]
    k = ticket_kpis(ticket, events)
    assert k["escalated"] is True and k["escalated_at"] == "2026-01-01T00:20:00+00:00"
    assert k["escalation_level"] == "tier2"
    assert k["resolved_at"] == "2026-01-01T03:00:00+00:00" and k["resolution_seconds"] == 10800.0
    assert k["rating"] == 5


def test_kpis_never_carry_free_text():
    k = ticket_kpis({"created": "2026-01-01T00:00:00+00:00", "status": "new", "subject": "SECRET"},
                    [_ev("replied", "2026-01-01T00:10:00+00:00", 1, by="operator")])
    assert "SECRET CONTENT" not in str(k) and "SECRET" not in str(k)   # no body, no subject
