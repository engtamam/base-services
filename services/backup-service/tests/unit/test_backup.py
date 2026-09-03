"""Export and import a service's data.

The happy-path roundtrip is the least of it. The tests that matter are the ones that decide whether
a restore can be trusted: a corrupt archive refused rather than restored as garbage, a wrong backup
key refused rather than producing nonsense, a re-run that changes nothing, and a restore that does
not bulldoze rows written since the backup. Those are the failures that turn "we have backups" into
a false statement.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core import backup as B

DECL = {
    "object_prefix": "",
    "collections": [
        {"name": "widgets", "tenant_scoped": True},
        {"name": "catalog", "tenant_scoped": False},
    ],
    "excluded": [],
}


def run(c):
    return asyncio.run(c)


def _seed(st, tenant="acme"):
    for i in range(3):
        run(st.put("widgets", f"w_{tenant}_{i}",
                   {"id": f"w_{tenant}_{i}", "tenant": tenant, "n": i, "created": f"2026-07-0{i+1}"}))
    run(st.put("catalog", "c1", {"id": "c1", "name": "Basic", "created": "2026-07-01"}))
    return st


def _export(st, tenant="", kek="backup-key"):
    seal, _ = B.make_sealer(kek)
    return run(B.export(st, DECL, service="thing-service", version="1.0.0",
                        taken_at="2026-07-22T00:00:00Z", seal=seal, tenant=tenant, scan_cap=1000))


# ── roundtrip ─────────────────────────────────────────────────────────────────
def test_tenants_covered_lists_the_distinct_tenants_in_the_archive():
    # approach (a): a whole-service export reports the distinct tenants its tenant-scoped rows hold,
    # in the clear, so a run can answer "was tenant X in run Y" with no per-tenant archive.
    st = MemoryDataStore()
    _seed(st, tenant="acme")
    _seed(st, tenant="globex")
    archive = _export(st)                              # whole service
    assert archive["tenants_covered"] == ["acme", "globex"]   # sorted, distinct


def test_tenants_covered_is_empty_for_a_purely_global_service():
    st = MemoryDataStore()
    run(st.put("catalog", "c1", {"id": "c1", "name": "Basic"}))   # only the GLOBAL collection
    archive = _export(st)
    assert archive["tenants_covered"] == []            # global rows have no tenant to cover


def test_tenants_covered_of_a_per_tenant_export_is_just_that_tenant():
    st = MemoryDataStore()
    _seed(st, tenant="acme")
    _seed(st, tenant="globex")
    archive = _export(st, tenant="acme")               # one customer's export
    assert archive["tenants_covered"] == ["acme"]      # never leaks globex


def test_a_backup_restores_into_an_empty_store():
    src = _seed(MemoryDataStore())
    archive = _export(src)

    _, unseal = B.make_sealer("backup-key")
    dst = MemoryDataStore()
    report = run(B.import_archive(dst, archive, unseal=unseal))

    assert report["inserted"] == 4      # 3 widgets + 1 catalog row
    assert run(dst.get("widgets", "w_acme_1"))["n"] == 1
    assert run(dst.get("catalog", "c1"))["name"] == "Basic"


def test_the_payload_is_not_in_the_clear():
    """A backup at rest must be ciphertext. Row COUNTS are exposed on purpose; values are not."""
    archive = _export(_seed(MemoryDataStore()))
    blob = str(archive)
    assert "Basic" not in blob and "w_acme_1" not in blob
    assert archive["row_counts"] == {"widgets": 3, "catalog": 1}


# ── corruption is refused, not restored ───────────────────────────────────────
def test_a_truncated_archive_is_refused():
    """A process that died mid-write must not produce something a restore will happily apply."""
    archive = _export(_seed(MemoryDataStore()))
    archive["sealed"] = archive["sealed"][:-20]        # the tail was never written
    with pytest.raises(B.BackupError, match="checksum"):
        B.verify(archive)


def test_a_missing_checksum_is_refused():
    archive = _export(_seed(MemoryDataStore()))
    del archive["checksum"]
    with pytest.raises(B.BackupError, match="truncated"):
        B.verify(archive)


def test_a_foreign_format_is_refused():
    with pytest.raises(B.BackupError, match="not a"):
        B.verify({"format": "someone-elses/v9", "sealed": "x", "checksum": "y"})


def test_a_corrupt_archive_never_reaches_the_datastore():
    archive = _export(_seed(MemoryDataStore()))
    archive["checksum"] = "sha256:0000"
    _, unseal = B.make_sealer("backup-key")
    dst = MemoryDataStore()
    with pytest.raises(B.BackupError):
        run(B.import_archive(dst, archive, unseal=unseal))
    assert run(dst.get("catalog", "c1")) is None, "a refused archive wrote rows anyway"


# ── the wrong key is refused clearly ──────────────────────────────────────────
def test_restoring_with_the_wrong_backup_key_is_refused():
    """A KEK rotated since the backup must fail loudly, not decrypt to nonsense and write it."""
    archive = _export(_seed(MemoryDataStore()), kek="the-old-key")
    _, wrong = B.make_sealer("a-different-key")
    with pytest.raises(B.BackupError, match="key does not match"):
        run(B.import_archive(MemoryDataStore(), archive, unseal=wrong))


def test_an_empty_backup_key_is_refused_rather_than_sealing_in_the_clear():
    with pytest.raises(B.BackupError, match="required"):
        B.make_sealer("")


# ── idempotent, and does not clobber ──────────────────────────────────────────
def test_restoring_twice_changes_nothing_the_second_time():
    archive = _export(_seed(MemoryDataStore()))
    _, unseal = B.make_sealer("backup-key")
    dst = MemoryDataStore()
    run(B.import_archive(dst, archive, unseal=unseal))
    second = run(B.import_archive(dst, archive, unseal=unseal))
    assert second["inserted"] == 0 and second["skipped"] == 4


def test_a_row_written_since_the_backup_is_not_bulldozed():
    """A restore into a partly-existing tenant must not overwrite what was written after the backup
    — that is what makes a half-finished restore safe to re-run."""
    archive = _export(_seed(MemoryDataStore()))
    _, unseal = B.make_sealer("backup-key")

    dst = MemoryDataStore()
    run(dst.put("widgets", "w_acme_1", {"id": "w_acme_1", "tenant": "acme", "n": 999}))
    report = run(B.import_archive(dst, archive, unseal=unseal))

    assert run(dst.get("widgets", "w_acme_1"))["n"] == 999, "a newer row was clobbered"
    assert report["skipped"] >= 1 and report["inserted"] >= 1


def test_overwrite_replaces_when_the_operator_asks():
    archive = _export(_seed(MemoryDataStore()))
    _, unseal = B.make_sealer("backup-key")
    dst = MemoryDataStore()
    run(dst.put("widgets", "w_acme_1", {"id": "w_acme_1", "tenant": "acme", "n": 999}))
    report = run(B.import_archive(dst, archive, unseal=unseal, overwrite=True))
    assert run(dst.get("widgets", "w_acme_1"))["n"] == 1
    assert report["overwritten"] >= 1


def test_a_row_with_no_identity_is_refused():
    """Restoring an id-less row twice would duplicate it — refuse rather than double-write."""
    seal, unseal = B.make_sealer("backup-key")
    st = MemoryDataStore()
    run(st.put("widgets", "ok", {"id": "ok", "tenant": "acme"}))
    # Forge an archive whose sealed body contains an id-less row.
    import json
    body = json.dumps({"service": "s", "tenant": "", "collections": {"widgets": [{"tenant": "acme"}]}})
    archive = {"format": B.ARCHIVE_FORMAT, "service": "s", "sealed": seal(body)}
    archive["checksum"] = B._checksum(archive["sealed"])
    with pytest.raises(B.BackupError, match="no id"):
        run(B.import_archive(MemoryDataStore(), archive, unseal=unseal))


# ── per-tenant scope ──────────────────────────────────────────────────────────
def test_a_per_tenant_backup_holds_only_that_tenant():
    st = MemoryDataStore()
    _seed(st, tenant="acme")
    _seed(st, tenant="globex")
    archive = _export(st, tenant="acme")

    _, unseal = B.make_sealer("backup-key")
    dst = MemoryDataStore()
    run(B.import_archive(dst, archive, unseal=unseal))

    rows, _ = run(dst.find("widgets", {"tenant": "acme"}, limit=100))
    assert rows and all(r["tenant"] == "acme" for r in rows)
    other, _ = run(dst.find("widgets", {"tenant": "globex"}, limit=100))
    assert other == [], "a per-tenant backup leaked another tenant's rows"


def test_a_per_tenant_backup_still_includes_global_collections():
    """A shared catalogue is not one tenant's to omit — a tenant restored without it is broken."""
    st = MemoryDataStore()
    _seed(st, tenant="acme")
    archive = _export(st, tenant="acme")
    assert archive["row_counts"]["catalog"] == 1


# ── verify_restore: prove the archive actually restores (Phase 46.4) ──────────
def _verify(st, archive, kek="backup-key", token="t1"):
    _, unseal = B.make_sealer(kek)
    return run(B.verify_restore(st, archive, unseal=unseal, token=token))


def test_verify_passes_for_a_good_archive_and_leaves_nothing_behind():
    src = _seed(MemoryDataStore())
    archive = _export(src)
    scratch = MemoryDataStore()

    verdict = _verify(scratch, archive)

    assert verdict["verified"] is True
    assert verdict["checked"] == verdict["expected"] == 4
    # The throwaway namespace is torn down — a verification never leaves probe rows in the store.
    leftover = run(scratch.scan_all("__bkpverify__t1__widgets", 1000))
    assert leftover == [], "verify left throwaway rows behind"
    assert run(scratch.scan_all("__bkpverify__t1__catalog", 1000)) == []


def test_verify_refuses_a_corrupt_archive_before_touching_the_store():
    src = _seed(MemoryDataStore())
    archive = _export(src)
    archive["checksum"] = "sha256:deadbeef"
    scratch = MemoryDataStore()
    with pytest.raises(B.BackupError):
        _verify(scratch, archive)


def test_verify_refuses_an_archive_the_key_cannot_open():
    """A KEK rotated since the backup: it decrypts to nothing usable, so it is NOT restorable."""
    src = _seed(MemoryDataStore())
    archive = _export(src, kek="the-old-key")
    with pytest.raises(B.BackupError):
        _verify(MemoryDataStore(), archive, kek="a-different-key")


class _TamperingStore(MemoryDataStore):
    """A store whose read-back does not match the write — stands in for a schema/serialisation drift
    that a checksum cannot see but a test-restore-and-compare must."""
    async def get(self, collection, key):
        row = await super().get(collection, key)
        if row is not None and collection.startswith("__bkpverify__"):
            row = {**row, "n": (row.get("n", 0) + 999)}   # not what was written
        return row


def test_verify_fails_when_a_row_does_not_round_trip():
    src = _seed(MemoryDataStore())
    archive = _export(src)
    verdict = _verify(_TamperingStore(), archive)
    assert verdict["verified"] is False
    assert verdict["mismatches"], "a row that restored differently was reported as verified"
