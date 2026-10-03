"""Regression tests for file-service audit findings #03/#04 (dedup) and #05 (stale sweep)."""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.object_store.memory import MemoryObjectStore
from app.core.file_repository import FileRepository
from app.core.file_service import FileService
from app.adapters.scanner.none import NoneScanner
from app.adapters.signed_url.hmac_signer import HmacSigner
from app.core.upload_session import PENDING, SCANNING, UploadSession, UploadSessionStore

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 40


def run(c):
    return asyncio.run(c)


def _svc():
    st = MemoryDataStore()
    obj = MemoryObjectStore()
    obj.supports_presign = True                          # enable the large-upload path for the test
    svc = FileService(repo=FileRepository(st), object_store=obj, scanner=NoneScanner(),
                      signed_url=HmacSigner("s" * 32, 300), max_upload_bytes=10_000_000,
                      allowed_content_types=frozenset(), sessions=UploadSessionStore(st))
    return svc, obj, st


# ── 04: dedup records the EXISTING file's size, not the client's unverified claim ──
def test_large_upload_dedup_uses_verified_size_not_client_claim():
    svc, obj, st = _svc()
    first = run(svc.upload(tenant="t1", owner="o", filename="a.png", content_type="image/png",
                           data=PNG))
    # begin a large upload for the SAME owner+content, but LIE about the size
    res = run(svc.begin_large_upload(tenant="t1", owner="o", filename="copy.png",
                                     content_type="image/png", sha256=first.sha256,
                                     size=999_999))
    assert res.deduped_file is not None
    assert res.deduped_file.size == len(PNG)            # verified size, not 999999
    assert res.deduped_file.content_type == "image/png"


# ── 03: dedup after the blob was deleted does NOT hand back a dangling file ────
def test_dedup_falls_through_when_blob_is_gone():
    import pytest
    from app.ports.errors import Unsupported
    svc, obj, st = _svc()
    first = run(svc.upload(tenant="t1", owner="o", filename="a.png", content_type="image/png",
                           data=PNG))
    run(obj.delete(first.sha256))                        # simulate the blob having been removed
    # must NOT dedup to a missing blob — it falls through to the real upload path (which the memory
    # store cannot presign, so it raises rather than returning a dangling deduped file).
    with pytest.raises(Unsupported):
        run(svc.begin_large_upload(tenant="t1", owner="o", filename="copy.png",
                                   content_type="image/png", sha256=first.sha256, size=len(PNG)))
    # and the speculative incr_ref was undone — ref count is unchanged (still 1 from the first upload)
    assert int(run(st.get("blob_refs", first.sha256))["count"]) == 1


# ── 05: a stuck SCANNING session is still reclaimable (file-009: at a longer, lease-aware cutoff) ──
def test_find_stale_can_target_scanning_sessions():
    st = MemoryDataStore()
    sessions = UploadSessionStore(st)
    run(sessions.create(UploadSession(id="s1", tenant="t1", owner="o", filename="f",
                                      content_type="application/octet-stream", declared_sha256="a" * 64,
                                      declared_size=1, upload_id="u1", part_size=1, part_count=1,
                                      status=SCANNING, created="2020-01-01T00:00:00Z")))
    # file-009: the TTL sweep no longer yanks SCANNING at the short PENDING cutoff — a worker may hold a
    # live lease — but a genuinely stuck one is reclaimable via an explicit SCANNING query at a longer cutoff.
    pending_only, _ = run(sessions.find_stale([PENDING], "2030-01-01T00:00:00Z"))
    assert not any(s["id"] == "s1" for s in pending_only)  # not swept by the PENDING-only sweep
    scanning, _ = run(sessions.find_stale([SCANNING], "2030-01-01T00:00:00Z"))
    assert any(s["id"] == "s1" for s in scanning)          # reclaimable at the scanning cutoff
