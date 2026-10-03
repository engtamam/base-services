"""Non-security MEDIUM remediation (compliance-service):
  007  ConsentSweeper write-back retires the legacy-key twin (no duplicate, no nightly re-announce).
  008  DsarStore.for_subject pushes {tenant, subject} down (a subject's older DSARs are never dropped).
  012  retention protects a PSEUDONYMISED proof row via the hold's subject_ref.
  014  the cross-tenant open-DSAR triage view pseudonymises the subject and omits the payload.
  015  a granted delegated-consent result is not cached past its majority lapse.
"""
from __future__ import annotations

import asyncio

from app.adapters.clock.manual import ManualClock
from app.adapters.datastore.memory import MemoryDataStore
from app.core.consent_service import ConsentService
from app.core.consent_sweeper import ConsentSweeper
from app.core.dsar_service import DsarService
from app.core.models import Dsar, LegalHold, new_id
from app.core.pii import subject_ref
from app.core.retention import DAY_SECONDS, RetentionEnforcer
from app.core.stores import ConsentStore, DsarStore, HoldStore, TargetStore

SALT = "s" * 32


def run(c):
    return asyncio.run(c)


class _Clock:
    def __init__(self, t):
        self.t = t

    def now(self):
        return self.t


# ── 007: sweeper retires the legacy-key twin ──────────────────────────────────
def test_sweeper_write_back_deletes_the_legacy_key_twin():
    store = MemoryDataStore()
    clock = _Clock(1_700_000_000.0)

    class _Bus:
        def __init__(self): self.n = 0
        async def publish(self, e, p): self.n += 1

    bus = _Bus()
    # A pre-normalisation row stored under the RAW-purpose (uppercase) key, already lapsed.
    legacy_key = "acme::kid@example.com::Coaching"
    row = {"tenant": "acme", "subject": "kid@example.com", "purpose": "Coaching",
           "basis": "guardian", "granted": True, "expires_at": clock.now() - 10}
    run(store.put("consent", legacy_key, row))

    sw = ConsentSweeper(store=store, consents=None, clock=clock, event_bus=bus, max_per_run=1000)
    run(sw.run())
    rows = run(store.scan_all("consent", limit=100))
    assert len(rows) == 1                                   # no duplicate
    assert rows[0]["id"] == "acme::kid@example.com::coaching"   # canonical key only
    assert bus.n == 1                                       # announced once

    run(sw.run())                                           # a second nightly run
    assert bus.n == 1                                       # must NOT re-announce


# ── 008: a subject's DSARs are fetched by pushed-down filter ───────────────────
def _dsar_service(store):
    return DsarService(dsars=DsarStore(store), holds=HoldStore(store), targets=TargetStore(store),
                       orchestrator=None, clock=_Clock(1_000_000.0),
                       config={"list_limit": 100, "pii_salt": SALT})


def test_for_subject_returns_only_that_subjects_dsars():
    store = MemoryDataStore()
    for i in range(3):
        d = Dsar(id=new_id("dsar_"), tenant="acme", subject="u_victim", type="access",
                 created_at=1_000 + i)
        run(DsarStore(store).save(d))
    other = Dsar(id=new_id("dsar_"), tenant="acme", subject="u_other", type="access", created_at=1_010)
    run(DsarStore(store).save(other))

    rows, _ = run(_dsar_service(store).list_for_subject("acme", "u_victim"))
    assert len(rows) == 3
    assert all(r["subject"] == "u_victim" for r in rows)


# ── 012: retention protects a pseudonymised proof row by subject_ref ───────────
def test_retention_hold_protects_a_pseudonymised_proof_row():
    raw = "person@example.com"
    ref = subject_ref(SALT, "acme", raw)
    old = _clock = 1_000_000_000.0
    store = MemoryDataStore()
    # a surviving (pseudonymised) proof row, aged well past the window
    proof = {"id": "pf_1", "tenant": "acme", "subject": ref,
             "created_at": old - 400 * DAY_SECONDS, "completed_at": old - 400 * DAY_SECONDS,
             "status": "completed"}
    run(store.put("proof", "pf_1", proof))
    # an active hold on the RAW person
    run(HoldStore(store).save(LegalHold(id="h_1", tenant="acme", subject=raw, active=True)))

    enf = RetentionEnforcer(store=store, holds=HoldStore(store), rules={"proof": 365},
                            clock=_Clock(old), pii_salt=SALT)
    out = run(enf.run())
    assert out.get("purged", {}) == {}                      # not deleted
    assert out["held_by_legal_hold"] == {"proof": 1}        # held by the person's hold


def test_retention_purges_a_pseudonymised_row_with_no_hold():
    store = MemoryDataStore()
    old = 1_000_000_000.0
    ref = subject_ref(SALT, "acme", "gone@example.com")
    proof = {"id": "pf_2", "tenant": "acme", "subject": ref,
             "created_at": old - 400 * DAY_SECONDS, "completed_at": old - 400 * DAY_SECONDS,
             "status": "completed"}
    run(store.put("proof", "pf_2", proof))
    enf = RetentionEnforcer(store=store, holds=HoldStore(store), rules={"proof": 365},
                            clock=_Clock(old), pii_salt=SALT)
    out = run(enf.run())
    assert out["purged"] == {"proof": 1}


# ── 014: cross-tenant triage pseudonymises and drops the payload ──────────────
def test_list_all_open_triage_hides_raw_subject_and_payload():
    store = MemoryDataStore()
    d = Dsar(id=new_id("dsar_"), tenant="acme", subject="person@example.com", type="erasure",
             status="pending", created_at=1_000, deadline_at=2_000,
             payload={"secret": "correction data"})
    run(DsarStore(store).save(d))
    rows, _ = run(_dsar_service(store).list_all_open())
    assert len(rows) == 1
    item = rows[0]
    assert item["subject_ref"] == subject_ref(SALT, "acme", "person@example.com")
    assert "subject" not in item and "payload" not in item
    assert item["tenant"] == "acme" and item["type"] == "erasure"


# ── 015: a granted delegated result is not cached past its lapse ───────────────
class _RecordingCache:
    def __init__(self):
        self.store: dict = {}
        self.set_calls: list[tuple[str, int]] = []

    async def get(self, k):
        return self.store.get(k)

    async def set(self, k, v, ttl):
        self.set_calls.append((k, ttl))
        self.store[k] = v


def test_cache_ttl_is_clamped_to_an_imminent_majority_lapse():
    from app.core.capacity import YEAR_SECONDS, CapacityPolicy
    cache = _RecordingCache()
    clock = ManualClock(1_000_000.0)
    cap = CapacityPolicy(age_of_majority=18, unknown_age=0)
    svc = ConsentService(store=ConsentStore(MemoryDataStore()), clock=clock, cache=cache,
                         cache_ttl=300, capacity=cap)
    # a delegated consent whose subject turns 18 in ~100s (inside the 300s TTL)
    majority = clock.now() + 100
    born = majority - 18 * YEAR_SECONDS
    run(svc.grant(tenant="t1", subject="kid", purpose="coaching",
                  basis="guardian", consented_by="parent", subject_born_at=born))
    cache.set_calls.clear()
    res = run(svc.check(tenant="t1", subject="kid", purpose="coaching"))
    assert res["allowed"] is True
    # the granted result was cached with a ttl clamped to the lapse, not the full 300s
    ttls = [t for _, t in cache.set_calls]
    assert ttls and all(0 < t <= 100 for t in ttls)
