"""Round-2 adversarial audit fixes (backup-service).

Each test maps to a confirmed finding in docs/audits/round2/backup-service.json and fails pre-fix.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core import backup as B
from app.core.internal_http import TargetUnreachable, _NoRedirect


def run(c):
    return asyncio.run(c)


_GLOBAL_DECL = {"object_prefix": "", "collections": [{"name": "plans", "tenant_scoped": False}],
                "excluded": []}
_TENANT_DECL = {"object_prefix": "", "collections": [{"name": "widgets", "tenant_scoped": True}],
                "excluded": []}


# r2-02 — a global collection over scan_cap FAILS the export instead of sealing a truncated archive.
def test_global_collection_over_cap_fails_instead_of_truncating():
    st = MemoryDataStore()
    for i in range(150):
        run(st.put("plans", f"p{i}", {"id": f"p{i}", "n": i}))
    seal, _ = B.make_sealer("k")
    with pytest.raises(B.BackupError):
        run(B.export(st, _GLOBAL_DECL, service="s", version="1", taken_at="t", seal=seal,
                     scan_cap=100))
    # under the cap it seals normally
    archive = run(B.export(st, _GLOBAL_DECL, service="s", version="1", taken_at="t", seal=seal,
                           scan_cap=1000))
    assert archive["row_counts"]["plans"] == 150


# r2-03 — a token-bearing egress call refuses to follow redirects (SSRF / token leak).
def test_redirects_are_refused():
    h = _NoRedirect()
    with pytest.raises(TargetUnreachable):
        h.redirect_request(req=None, fp=None, code=302, msg="Found", headers={},
                           newurl="http://evil.example/steal")


# r2-04 — into_tenant is refused on a whole-service archive (would comingle every tenant).
def test_into_tenant_refused_on_whole_service_archive():
    st = MemoryDataStore()
    for t in ("acme", "globex"):
        run(st.put("widgets", f"w-{t}", {"id": f"w-{t}", "tenant": t}))
    seal, unseal = B.make_sealer("k")
    whole = run(B.export(st, _TENANT_DECL, service="s", version="1", taken_at="t", seal=seal,
                         scan_cap=1000))            # tenant="" → whole-service, multi-tenant
    dst = MemoryDataStore()
    with pytest.raises(B.BackupError):
        run(B.import_archive(dst, whole, unseal=unseal, into_tenant="victim"))


def test_into_tenant_allowed_on_single_tenant_archive():
    st = MemoryDataStore()
    run(st.put("widgets", "w1", {"id": "w1", "tenant": "acme"}))
    seal, unseal = B.make_sealer("k")
    per = run(B.export(st, _TENANT_DECL, service="s", version="1", taken_at="t", seal=seal,
                       tenant="acme", scan_cap=1000))
    dst = MemoryDataStore()
    out = run(B.import_archive(dst, per, unseal=unseal, into_tenant="sandbox"))
    assert out["remapped"] == 1
    row = run(dst.get("widgets", "w1"))
    assert row["tenant"] == "sandbox"


# r2-01 — prune makes a restore authoritative: rows absent from the archive are deleted (true undo).
def test_prune_deletes_rows_absent_from_the_archive():
    src = MemoryDataStore()
    run(src.put("widgets", "w1", {"id": "w1", "tenant": "acme", "n": 1}))
    seal, unseal = B.make_sealer("k")
    snapshot = run(B.export(src, _TENANT_DECL, service="s", version="1", taken_at="t", seal=seal,
                            scan_cap=1000))          # snapshot of prior state: {w1}

    target = MemoryDataStore()
    run(target.put("widgets", "w1", {"id": "w1", "tenant": "acme", "n": 1}))
    run(target.put("widgets", "w2", {"id": "w2", "tenant": "acme", "n": 2}))  # inserted by a bad restore

    out = run(B.import_archive(target, snapshot, unseal=unseal, overwrite=True, prune=True,
                              scan_cap=1000))
    assert out["pruned"] == 1                         # w2 removed
    assert run(target.get("widgets", "w2")) is None   # authoritative rollback
    assert run(target.get("widgets", "w1")) is not None


def test_prune_refuses_a_truncated_current_view():
    target = MemoryDataStore()
    for i in range(150):
        run(target.put("widgets", f"w{i}", {"id": f"w{i}", "tenant": "acme"}))
    src = MemoryDataStore()
    seal, unseal = B.make_sealer("k")
    snapshot = run(B.export(src, _TENANT_DECL, service="s", version="1", taken_at="t", seal=seal,
                            scan_cap=1000))          # empty snapshot
    with pytest.raises(B.BackupError):
        run(B.import_archive(target, snapshot, unseal=unseal, overwrite=True, prune=True,
                            scan_cap=100))            # 150 current > cap → refuse to half-prune
