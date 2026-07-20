"""ScanWorker — single-pass hash+scan, quarantine on reject, skip-policy, and the boot guard."""
from __future__ import annotations

import hashlib

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.scanner.magic import MagicScanner
from app.adapters.scanner.none import NoneScanner
from app.config import Settings
from app.core.file_repository import FileRepository
from app.core.file_service import FileService
from app.core.scan_worker import ScanWorker
from app.core.upload_session import QUARANTINED, READY, UploadSessionStore
from app.ports.object_store import CompletedPart
from tests.contracts.util import run
from tests.fakes import FakePresignStore

_REFS = "blob_refs"


# The REAL memory adapter, not a stand-in. A hand-written fake queue is what let the pop-vs-lease
# bug live here unnoticed: the fake implemented `dequeue` and nothing else, so every test passed
# while measuring a contract the shipped adapters do not have. Using the real driver means these
# tests break the day the port changes — which is exactly what happened when it did.
from app.adapters.queue.memory import MemoryQueue


def _wire(scanner, *, policy="async"):
    ds = MemoryDataStore()
    store = FakePresignStore()
    queue = MemoryQueue()
    sessions = UploadSessionStore(ds)
    files = FileService(
        repo=FileRepository(ds), object_store=store, scanner=scanner, signed_url=None,
        max_upload_bytes=25 << 20, allowed_content_types=frozenset(),
        sessions=sessions, queue=queue, scan_queue="file-scan",
        max_object_bytes=5 << 30, part_size=8 << 20, scan_policy=policy,
    )
    worker = ScanWorker(files=files, sessions=sessions, object_store=store, scanner=scanner,
                        queue=queue, scan_queue="file-scan", scan_policy=policy,
                        lease_seconds=900)
    return files, worker, store, ds, sessions


def _do_upload(files, store, data, sha, ct=""):
    begin = run(files.begin_large_upload(tenant="t", owner="u", filename="f", content_type=ct,
                                         sha256=sha, size=len(data)))
    run(store.put(begin.session.staging_key, data))
    run(files.complete_large_upload(session_id=begin.session.id, tenant="t", owner="u",
                                    parts=[CompletedPart(1, "e1")]))
    return begin.session


def test_worker_hashes_and_promotes_a_clean_object():
    files, worker, store, ds, sessions = _wire(NoneScanner())
    data = b"clean-video" * 20_000
    sha = hashlib.sha256(data).hexdigest()
    sess = _do_upload(files, store, data, sha)
    processed = run(worker.run_once())
    assert processed == 1
    out = run(sessions.get(sess.id))
    assert out.status == READY
    assert run(store.get(sha)) == data
    assert run(ds.get(_REFS, sha))["count"] == 1


def test_worker_quarantines_a_digest_mismatch_computed_from_the_real_bytes():
    files, worker, store, ds, sessions = _wire(NoneScanner())
    data = b"real-bytes" * 3000
    run_sess = _do_upload(files, store, data, "c" * 64)   # declared hash is a lie
    run(worker.run_once())
    out = run(sessions.get(run_sess.id))
    assert out.status == QUARANTINED and out.reason == "digest_mismatch"
    assert not run(store.exists("c" * 64))


def test_worker_quarantines_on_scanner_reject_via_streaming_scan():
    files, worker, store, ds, sessions = _wire(MagicScanner(strict=True))
    data = b"MZ" + b"\x00" * 5000                          # DOS/PE executable signature
    sha = hashlib.sha256(data).hexdigest()
    sess = _do_upload(files, store, data, sha, ct="application/octet-stream")
    run(worker.run_once())
    out = run(sessions.get(sess.id))
    assert out.status == QUARANTINED
    assert "executable" in out.reason
    assert not run(store.exists(sha))                      # content key never written


def test_streaming_magic_scan_accepts_a_matching_type():
    files, worker, store, ds, sessions = _wire(MagicScanner(strict=True))
    data = b"%PDF-1.7\n" + b"body" * 5000
    sha = hashlib.sha256(data).hexdigest()
    sess = _do_upload(files, store, data, sha, ct="application/pdf")
    run(worker.run_once())
    assert run(sessions.get(sess.id)).status == READY


def test_skip_policy_verifies_hash_but_does_not_scan():
    # An executable that a real scanner would reject is allowed through under skip — but the hash is
    # still verified, so integrity holds. (skip is refused in production by the boot guard.)
    files, worker, store, ds, sessions = _wire(MagicScanner(strict=True), policy="skip")
    data = b"MZ" + b"\x00" * 5000
    sha = hashlib.sha256(data).hexdigest()
    sess = _do_upload(files, store, data, sha, ct="application/octet-stream")
    run(worker.run_once())
    assert run(sessions.get(sess.id)).status == READY      # not scanned → not rejected


def test_boot_guard_refuses_skip_in_production():
    prod = Settings(ENV="production", LARGE_UPLOAD_SCAN="skip", INTERNAL_SERVICE_TOKEN="x" * 32)
    with pytest.raises(ValueError, match="skip is refused in production"):
        prod.assert_large_scan_ok()
    # async/require are fine in production
    Settings(ENV="production", LARGE_UPLOAD_SCAN="async",
             INTERNAL_SERVICE_TOKEN="x" * 32).assert_large_scan_ok()


def test_require_policy_refuses_a_large_upload_without_a_real_scanner():
    from app.core.file_service import LargeUploadUnavailable
    files, worker, store, ds, sessions = _wire(NoneScanner(), policy="require")
    with pytest.raises(LargeUploadUnavailable, match="require"):
        run(files.begin_large_upload(tenant="t", owner="u", filename="f", content_type="",
                                     sha256="a" * 64, size=100))
