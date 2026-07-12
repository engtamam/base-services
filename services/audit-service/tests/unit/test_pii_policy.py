"""The policy that makes the pseudonym rule true instead of merely intended.

A rule that depends on every caller in every service remembering to pseudonymise is not a rule.
One forgotten call site and the person is in the hash chain forever, and there is no fix — you
cannot redact a hashed record without breaking `verify()` from that seq onward.

So these tests are about the boundary, not the callers: whatever anybody sends, what actually gets
hashed must be safe. Edge cases included, because the edge cases are where the person ends up.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.integrity.hash_chain import HashChainIntegrity
from app.core.audit_repository import AuditRepository
from app.core.audit_service import AuditService
from app.core.pii import is_subject_ref, subject_ref
from app.core.pii_policy import ENFORCE, FOLD, OFF, PiiPolicy, PiiPolicyError, parse_fields
from app.ports.errors import ConfigError

SALT = "product-pii-salt"
TENANT = "t1"
FIELDS = parse_fields("email,phone,name,username,address,ip,national_id,iqama,passport")


def run(c):
    return asyncio.run(c)


def _svc(mode=FOLD, salt=SALT, fields=FIELDS):
    store = MemoryDataStore()
    return AuditService(
        repo=AuditRepository(store), integrity=HashChainIntegrity(), exporters={},
        default_format="json", partition_mode="global", query_limit=100,
        pii_policy=PiiPolicy(mode=mode, fields=fields, salt=salt),
    ), store


def _policy(mode=FOLD, salt=SALT, fields=FIELDS):
    return PiiPolicy(mode=mode, fields=fields, salt=salt)


# ── fold: safe by default, whatever the caller forgets ──────────────────────
def test_fold_pseudonymises_a_raw_actor_so_pii_cannot_land():
    """THE guarantee. A caller sends an email; the chain gets a pseudonym."""
    svc, store = _svc(FOLD)
    run(svc.record(service="identity", action="login", actor="alice@x.com", tenant=TENANT))

    rows = run(store.scan_all("audit_events", 10))
    blob = str(rows)
    assert "alice@x.com" not in blob, "a raw email reached the hash chain — permanent, unerasable"
    assert subject_ref(SALT, TENANT, "alice@x.com") in blob


def test_fold_leaves_an_existing_pseudonym_alone():
    """Never double-wrap: sub_(sub_(x)) would break every lookup."""
    ref = subject_ref(SALT, TENANT, "alice@x.com")
    actor, _, _ = _policy(FOLD).apply(tenant=TENANT, actor=ref, ip="", metadata=None)
    assert actor == ref


def test_fold_redacts_personal_metadata_fields():
    svc, store = _svc(FOLD)
    run(svc.record(service="s", action="a", actor="", tenant=TENANT,
                   metadata={"email": "bob@x.com", "order_id": "ord_1"}))
    blob = str(run(store.scan_all("audit_events", 10)))
    assert "bob@x.com" not in blob, "metadata is where PII actually sneaks in"
    assert "ord_1" in blob, "a non-personal field was redacted — the record lost its meaning"


def test_fold_drops_the_ip_because_an_ip_is_personal_data():
    """The field people forget, because it does not look like a name (GDPR Rec. 30)."""
    _, ip, _ = _policy(FOLD).apply(tenant=TENANT, actor="", ip="203.0.113.9", metadata=None)
    assert ip == ""


def test_fold_without_a_salt_fails_at_BOOT_not_per_request():
    """An unsalted pseudonym is reversible by guessing every email on earth.

    A service that cannot pseudonymise cannot honestly claim it can erase anyone — so it must
    refuse to START, not fail one request at 3am long after it was deployed.
    """
    with pytest.raises(ConfigError, match="PII_SALT"):
        PiiPolicy(mode=FOLD, fields=FIELDS, salt="")


def test_off_mode_needs_no_salt():
    """The unsafe mode is at least honest: it does not pretend to pseudonymise."""
    PiiPolicy(mode=OFF, fields=FIELDS, salt="")   # does not raise


# ── enforce: refuse the write ───────────────────────────────────────────────
def test_enforce_refuses_a_raw_actor():
    with pytest.raises(PiiPolicyError, match="subject_ref"):
        _policy(ENFORCE).apply(tenant=TENANT, actor="alice@x.com", ip="", metadata=None)


def test_enforce_accepts_a_pseudonym():
    ref = subject_ref(SALT, TENANT, "alice@x.com")
    actor, _, _ = _policy(ENFORCE).apply(tenant=TENANT, actor=ref, ip="", metadata=None)
    assert actor == ref


def test_enforce_refuses_personal_metadata():
    with pytest.raises(PiiPolicyError, match="personal fields"):
        _policy(ENFORCE).apply(tenant=TENANT, actor="", ip="", metadata={"phone": "+9665..."})


# ── off: legacy, and the ONLY way to violate the guarantee ──────────────────
def test_off_is_a_passthrough_and_is_therefore_unsafe():
    actor, ip, meta = _policy(OFF).apply(tenant=TENANT, actor="alice@x.com", ip="1.2.3.4",
                                         metadata={"email": "b@x.com"})
    assert actor == "alice@x.com" and ip == "1.2.3.4" and meta["email"] == "b@x.com"


# ── config, not code ────────────────────────────────────────────────────────
def test_the_pii_vocabulary_is_configurable_per_platform():
    """A Saudi platform needs national_id/iqama; a health platform needs mrn. Not our call."""
    policy = PiiPolicy(mode=FOLD, fields=parse_fields("mrn,iqama"), salt=SALT)
    _, _, meta = policy.apply(tenant=TENANT, actor="", ip="",
                              metadata={"mrn": "123", "iqama": "456", "email": "e@x.com"})
    assert meta["mrn"] == "[redacted]" and meta["iqama"] == "[redacted]"
    # `email` is NOT in this platform's list, so it is left alone — the platform decides, we do not
    assert meta["email"] == "e@x.com"


def test_parse_fields_tolerates_whitespace_and_case():
    assert parse_fields(" Email , PHONE ,, national_id ") == ("email", "phone", "national_id")


def test_an_unknown_mode_fails_fast_at_boot():
    """Like every other driver choice here: an unknown value is a ConfigError, not a default."""
    with pytest.raises(ConfigError, match="AUDIT_PII_MODE"):
        PiiPolicy(mode="maybe", fields=FIELDS, salt=SALT)


# ── edge cases: where the person actually ends up ──────────────────────────
def test_an_empty_actor_is_left_empty():
    """System events have no subject. They must not be forced to invent one."""
    actor, _, _ = _policy(FOLD).apply(tenant=TENANT, actor="", ip="", metadata=None)
    assert actor == ""


def test_metadata_none_is_handled():
    _, _, meta = _policy(FOLD).apply(tenant=TENANT, actor="", ip="", metadata=None)
    assert meta == {}


def test_metadata_key_case_is_not_a_bypass():
    """`Email` must not slip past a lowercase list. Case is the oldest bypass there is."""
    _, _, meta = _policy(FOLD).apply(tenant=TENANT, actor="", ip="",
                                     metadata={"Email": "a@x.com", "PHONE": "+1"})
    assert meta["Email"] == "[redacted]" and meta["PHONE"] == "[redacted]"


def test_the_same_person_in_two_tenants_gets_two_refs():
    """Cross-tenant correlation of one human would be a privacy leak in itself."""
    a = subject_ref(SALT, "t1", "alice@x.com")
    b = subject_ref(SALT, "t2", "alice@x.com")
    assert a != b


def test_folding_is_stable_so_the_record_stays_queryable():
    """If the ref changed per event you could never ask 'what did this subject do?'."""
    p = _policy(FOLD)
    first, _, _ = p.apply(tenant=TENANT, actor="alice@x.com", ip="", metadata=None)
    second, _, _ = p.apply(tenant=TENANT, actor="alice@x.com", ip="", metadata=None)
    assert first == second and is_subject_ref(first)


def test_the_chain_still_verifies_with_the_policy_applied():
    """Folding must not disturb the hash chain it exists to protect."""
    svc, _ = _svc(FOLD)
    for i in range(15):
        run(svc.record(service="s", action="a", actor=f"user{i}@x.com", tenant=TENANT,
                       metadata={"email": "leak@x.com", "keep": i}))
    report = run(svc.verify())
    assert report["ok"], "the chain broke once the PII policy was applied"
