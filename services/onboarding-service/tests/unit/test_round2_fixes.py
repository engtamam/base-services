"""Round-2 fixes (onboarding-service). Maps to docs/audits/round2/onboarding-service.json."""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.grants import GrantService
from app.core.provisioning import ProvisioningService


def run(c):
    return asyncio.run(c)


class _Clock:
    def now(self):
        return 1000.0


class _Prov:
    driver = "fake"

    def __init__(self, kind):
        self.kind = kind

    async def create(self, req):
        from app.ports.provisioner import CREATED, ProvisionResult
        return ProvisionResult(status=CREATED, reference=f"{self.kind}-1")

    async def undo(self, req, ref):
        from app.ports.provisioner import CREATED, ProvisionResult
        return ProvisionResult(status=CREATED, reference=ref)


def _svc(store):
    return ProvisioningService(provisioners={"tenant": _Prov("tenant")}, store=store, clock=_Clock())


# r2-01 — a second concurrent provision for the same (tenant, subject) is refused, not interleaved.
def test_concurrent_provision_is_serialized():
    store = MemoryDataStore()
    svc = _svc(store)
    run_key = svc._run_key("acme", "u1")
    # simulate a LIVE in-flight run: a fresh lock carries a recent 'at' (onboarding-001).
    assert run(store.cas("provisioning_locks", run_key, 0,
                         {"id": run_key, "at": 1000.0})) is True
    out = run(svc.provision(tenant="acme", subject="u1", steps=[{"kind": "tenant"}]))
    assert out["ok"] is False and out["error"] == "provisioning_in_progress"
    # once the lock is released, provisioning proceeds
    run(store.delete("provisioning_locks", run_key))
    ok = run(svc.provision(tenant="acme", subject="u1", steps=[{"kind": "tenant"}]))
    assert ok["ok"] is True


# onboarding-001 — a STALE crash-orphan lock (old 'at', no finally ran) is reclaimed, not wedged.
def test_stale_provision_lock_is_reclaimed():
    store = MemoryDataStore()
    svc = _svc(store)
    run_key = svc._run_key("acme", "u1")
    # a crash left a lock whose 'at' is far in the past (well beyond the lease)
    assert run(store.cas("provisioning_locks", run_key, 0, {"id": run_key, "at": 0.0})) is True
    out = run(svc.provision(tenant="acme", subject="u1", steps=[{"kind": "tenant"}]))
    assert out["ok"] is True                     # reclaimed the orphan and provisioned


# r2-02 — a later clean run does NOT erase an earlier run's outstanding unfinished/residue queue.
def test_run_record_merges_prior_unfinished():
    store = MemoryDataStore()
    svc = _svc(store)
    run_key = svc._run_key("acme", "u2")
    # a prior run left an unfinished compensation for a step this run won't touch
    run(store.put("provisioning_runs", run_key,
                  {"id": run_key, "unfinished": [{"step": "old_cleanup", "why": "unreachable"}],
                   "residue": [{"step": "old_email", "why": "sent"}]}))
    out = run(svc.provision(tenant="acme", subject="u2", steps=[{"kind": "tenant"}]))
    assert out["ok"] is True
    steps = {e["step"] for e in out["unfinished"]}
    assert "old_cleanup" in steps                       # carried forward, not erased
    assert any(e["step"] == "old_email" for e in out["residue"])


# r2-07 — a grant with no once_per is "granted every time", not silently refused.
def test_grant_without_once_per_is_granted():
    class _Ent:
        async def issue(self, req):
            from app.ports.entitlement import GRANTED, Receipt
            return Receipt(status=GRANTED, reference="r1")

    class _Ledger:
        def __init__(self): self.rows = {}
        async def find(self, key): return self.rows.get(key)
        async def record(self, key, **kw): self.rows[key] = {"reference": kw.get("reference", "")}

    gs = GrantService(entitlement=_Ent(), ledger=_Ledger(), identity_lookup=None,
                      specs={"welcome": {"amount": 100, "currency": "SAR"}})
    r1 = run(gs.issue(kind="welcome", once_per="", tenant="acme", subject="u1"))
    r2 = run(gs.issue(kind="welcome", once_per="", tenant="acme", subject="u1"))
    from app.ports.entitlement import GRANTED
    assert r1.status == GRANTED and r2.status == GRANTED   # every time, never REFUSED
