"""Core file logic — validated upload, content-addressed storage, signed URLs.

Content-addressing: the blob key is sha256(bytes), so identical uploads dedup to
one blob; a ref-count makes delete safe (the blob is removed only when the last
referencing file is deleted).

Two upload models live here (Phase 34):

  * SMALL (`upload`): bytes through the app — scanned and hashed in-request, then stored. Unchanged.
  * LARGE (`begin_large_upload` → `complete_large_upload` → `finalize_large_upload`): the client
    uploads straight to the bucket via a presigned multipart plan; the bytes never touch this
    process. The object lands on a per-session STAGING key, is hashed and scanned asynchronously,
    and is promoted to its content-addressed key only when clean. A file is READY — ref-counted,
    dedup-eligible, downloadable — only after that verification, never on the client's word.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
from typing import Any

from app.core.file_repository import FileRepository
from app.core.models import FileObject, new_id
from app.core.upload_session import (
    ABORTED, PENDING, QUARANTINED, READY, SCANNING, UploadSession, UploadSessionStore,
)
from app.ports.errors import NotFound, Unsupported

_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


class TooLarge(Exception):
    pass


class TypeNotAllowed(Exception):
    pass


class BadDigest(Exception):
    """The declared sha256 is not a 64-char lowercase hex string."""


class LargeUploadUnavailable(Exception):
    """The wired object store cannot presign, so the direct-to-bucket path is not available."""


class BeginResult:
    """What `begin_large_upload` returns: either a dedup hit (no upload needed) or a presigned plan."""

    def __init__(self, *, session: UploadSession | None, plan: Any | None,
                 deduped_file: FileObject | None) -> None:
        self.session = session
        self.plan = plan
        self.deduped_file = deduped_file


class FileService:
    def __init__(self, *, repo: FileRepository, object_store, scanner, signed_url,
                 max_upload_bytes: int, allowed_content_types: frozenset[str],
                 max_page_limit: int = 100,
                 sessions: UploadSessionStore | None = None, queue: Any = None,
                 scan_queue: str = "file-scan", max_object_bytes: int = 0,
                 part_size: int = 8 << 20, scan_policy: str = "async",
                 presign_ttl: int = 900) -> None:
        self._repo = repo
        self._store = object_store
        self._scanner = scanner
        self._signed = signed_url
        self._max = max_upload_bytes
        self._allowed = allowed_content_types
        self._max_page = max_page_limit
        self._sessions = sessions
        self._queue = queue
        self._scan_queue = scan_queue
        self._max_object = max_object_bytes
        self._part_size = part_size
        self._scan_policy = scan_policy
        self._presign_ttl = int(presign_ttl)

    # ── large path (direct-to-bucket) ─────────────────────────────────────────
    @property
    def large_uploads_available(self) -> bool:
        return bool(getattr(self._store, "supports_presign", False)) and self._sessions is not None

    async def begin_large_upload(self, *, tenant: str, owner: str, filename: str,
                                 content_type: str, sha256: str, size: int) -> BeginResult:
        """Start a direct-to-bucket upload: validate, dedup if the owner already has it, else presign.

        The declared sha256 is a CLAIM, not yet trusted — it names the content key the object will
        take IF its bytes verify. The upload lands on a per-session staging key; nothing references
        the content key until `finalize_large_upload` confirms the hash and a clean scan.
        """
        if not self.large_uploads_available:
            raise LargeUploadUnavailable(
                "the wired object store cannot presign — large uploads need an s3 store")
        if self._scan_policy == "require" and getattr(self._scanner, "driver", "none") == "none":
            # Fail closed: the operator asked that large uploads be scanned, but no real scanner is
            # wired — so we refuse to start one rather than store an unscannable object.
            raise LargeUploadUnavailable(
                "LARGE_UPLOAD_SCAN=require but no scanner is configured (SCANNER_DRIVER=none)")
        sha = (sha256 or "").strip().lower()
        if not _SHA256_RE.match(sha):
            raise BadDigest(sha256)
        if size < 0 or (self._max_object and size > self._max_object):
            raise TooLarge(f"declared size exceeds {self._max_object} bytes")
        ct = (content_type or "application/octet-stream").split(";")[0].strip().lower()
        if self._allowed and ct not in self._allowed:
            raise TypeNotAllowed(ct)

        # Possession-safe dedup: only if THIS owner already holds the content. No upload, no scan —
        # they proved possession when they uploaded it the first time.
        existing = await self._repo.first_by_owner_sha(owner, sha)
        if existing is not None:
            # incr_ref FIRST: once the ref is bumped a concurrent delete's claim_blob_delete (ref==0)
            # can no longer remove the blob. But `exists()` alone is NOT enough (audit file-024): a
            # delete for the LAST other reference can have already passed del_if_zero (ref row removed)
            # and be about to physically delete the bytes; our incr then RESURRECTS the ref row at 1
            # while exists() still sees the not-yet-deleted blob → we would dedup onto bytes that vanish
            # a moment later (silent data loss).
            #
            # The tell is the returned count: a LIVE dedup target already holds >=1 reference (the
            # `existing` file), so our increment must observe >=2. A return of exactly 1 means the row
            # was absent when we incremented — a concurrent last-ref delete removed it — so the blob is
            # NOT safely held. Only dedup when the ref is STABLY >=2 AND the blob exists; otherwise undo
            # our ref and fall through to a real upload so the client re-supplies the content. (When our
            # +1 makes the count >=2 it can never reach 0 while we hold it, so del_if_zero cannot fire
            # and the bytes cannot be deleted out from under the new file.)
            new_ref = await self._repo.incr_ref(sha)
            if new_ref >= 2 and await self._store.exists(sha):
                # Use the EXISTING file's verified size/content_type, not the client's unverified
                # claim (the bytes are not re-hashed on this path).
                f = FileObject(id=new_id(), tenant=tenant, owner=owner, filename=filename,
                               content_type=existing.content_type, size=existing.size, sha256=sha)
                await self._repo.create(f)
                return BeginResult(session=None, plan=None, deduped_file=f)
            await self._repo.decr_ref(sha)   # resurrected row or blob gone — no dedup; fresh upload

        sess = UploadSession(id=new_id(), tenant=tenant, owner=owner, filename=filename,
                             content_type=ct, declared_sha256=sha, declared_size=size,
                             upload_id="", part_size=self._part_size, part_count=0)
        plan = await self._store.begin_multipart(sess.staging_key, ct, size, self._part_size)
        sess.upload_id = plan.upload_id
        sess.part_count = plan.part_count
        await self._sessions.create(sess)
        return BeginResult(session=sess, plan=plan, deduped_file=None)

    async def get_session(self, session_id: str, *, tenant: str, owner: str) -> UploadSession:
        sess = await self._sessions.get(session_id) if self._sessions else None
        if sess is None or sess.tenant != tenant or sess.owner != owner:
            raise NotFound(session_id)   # hide existence across owners
        return sess

    async def complete_large_upload(self, *, session_id: str, tenant: str, owner: str,
                                    parts: list[Any]) -> UploadSession:
        """Client finished uploading parts. Assemble, confirm it landed, then hand off to the scanner.

        Ref-count is NOT touched here — the bytes are unverified until the async scan runs. A size
        that does not match the declaration, or a missing object, quarantines immediately.
        """
        sess = await self.get_session(session_id, tenant=tenant, owner=owner)
        if sess.status != PENDING:
            return sess   # idempotent: a retried complete returns the current state
        await self._store.complete_multipart(sess.staging_key, sess.upload_id, parts)
        head = await self._store.head(sess.staging_key)
        if head is None or head.size != sess.declared_size:
            await self._discard_staging(sess)
            sess.status = QUARANTINED
            sess.reason = "object_missing" if head is None else "size_mismatch"
            return await self._sessions.save(sess)
        sess.status = SCANNING
        await self._sessions.save(sess)
        await self._queue.enqueue(self._scan_queue, {"session_id": sess.id})
        return sess

    async def finalize_large_upload(self, session_id: str, *, computed_sha: str,
                                    clean: bool, reason: str = "") -> UploadSession | None:
        """Called by the scan worker with the object's REAL hash and the scan verdict.

        Clean + hash-matches → promote staging to the content key, ref-count, create the FileObject.
        Otherwise → discard staging (never the content key) and quarantine. Idempotent: only a
        session still in `scanning` transitions.
        """
        sess = await self._sessions.get(session_id) if self._sessions else None
        if sess is None or sess.status != SCANNING:
            return sess
        digest_ok = (computed_sha or "").strip().lower() == sess.declared_sha256
        if not clean or not digest_ok:
            await self._discard_staging(sess)
            sess.status = QUARANTINED
            # A scan REJECT is a definite bad verdict (and the worker stopped hashing), so it wins;
            # only a fully-hashed-but-mismatched object is a digest_mismatch.
            sess.reason = (reason or "rejected") if not clean else "digest_mismatch"
            return await self._sessions.save(sess)
        # Verified + clean: promote to the content-addressed key, then reference it.
        await self._store.promote(sess.staging_key, sess.content_key)
        await self._repo.incr_ref(sess.content_key)
        f = FileObject(id=new_id(), tenant=sess.tenant, owner=sess.owner, filename=sess.filename,
                       content_type=sess.content_type, size=sess.declared_size,
                       sha256=sess.content_key)
        await self._repo.create(f)
        sess.status = READY
        sess.file_id = f.id
        return await self._sessions.save(sess)

    async def abort_session(self, session_id: str, *, tenant: str, owner: str) -> UploadSession:
        """Cancel a caller's own in-flight upload: abort the multipart, drop staging, mark aborted.

        Deleting a `pending` upload is not just a metadata delete — the multipart holds paid-for
        parts in the bucket that must be released.
        """
        sess = await self.get_session(session_id, tenant=tenant, owner=owner)
        if not sess.is_terminal:
            await self._discard_staging(sess)
            sess.status = ABORTED
            sess.reason = "aborted_by_owner"
            await self._sessions.save(sess)
        return sess

    async def sweep_stale_sessions(self, cutoff_iso: str, *, limit: int = 100) -> dict[str, Any]:
        """Abort every pending session older than `cutoff_iso` (its multipart + staging released).

        Internal, driven by a CronJob. Paged and bounded — a sweeper never scans the whole table.
        """
        aborted = 0
        cursor = None
        while True:
            rows, cursor = await self._sessions.find_stale_pending(cutoff_iso, limit=limit,
                                                                   after=cursor)
            for r in rows:
                sess = UploadSession.from_dict(r)
                await self._discard_staging(sess)
                sess.status = ABORTED
                sess.reason = "expired"
                await self._sessions.save(sess)
                aborted += 1
            if not cursor:
                break
        return {"aborted": aborted}

    async def _discard_staging(self, sess: UploadSession) -> None:
        """Remove a session's staging object (and any lingering multipart). Never the content key —
        a shared content blob is not this session's to delete."""
        try:
            await self._store.abort_multipart(sess.staging_key, sess.upload_id)
        except Exception:   # noqa: BLE001 — a completed/absent multipart is already gone
            pass
        try:
            await self._store.delete(sess.staging_key)
        except Exception:   # noqa: BLE001
            pass

    async def upload(self, *, tenant: str, owner: str, filename: str,
                     content_type: str, data: bytes) -> FileObject:
        if len(data) > self._max:
            raise TooLarge(f"upload exceeds {self._max} bytes")
        ct = (content_type or "application/octet-stream").split(";")[0].strip().lower()
        if self._allowed and ct not in self._allowed:
            raise TypeNotAllowed(ct)
        # OFFLOAD the blocking scan off the event loop (file-003): the clamav driver does a synchronous
        # socket round-trip to clamd over up to MAX_UPLOAD_BYTES, which would freeze the single-worker
        # loop for every other request. Mirrors how token verification and the streaming scan offload.
        await asyncio.to_thread(self._scanner.scan, data, ct)  # raises Rejected
        sha = hashlib.sha256(data).hexdigest()
        await self._store.put(sha, data, ct)          # idempotent (content-addressed)
        await self._repo.incr_ref(sha)                 # dedup ref-count
        f = FileObject(id=new_id(), tenant=tenant, owner=owner, filename=filename,
                       content_type=ct, size=len(data), sha256=sha)
        return await self._repo.create(f)

    async def get(self, file_id: str) -> FileObject:
        f = await self._repo.get(file_id)
        if f is None:
            raise NotFound(file_id)
        return f

    async def download(self, file_id: str) -> tuple[FileObject, bytes]:
        f = await self.get(file_id)
        return f, await self._store.get(f.sha256)

    async def list(self, tenant: str, limit: int = 100,
                   after: str | None = None) -> dict[str, Any]:
        """A page of one tenant's files, plus the cursor for the next.

        The limit is CLAMPED here. A caller does not get to choose 1 000 000 — that is how the
        whole table ended up in memory in the first place.
        """
        limit = max(1, min(int(limit), self._max_page))
        rows, next_cursor = await self._repo.list(tenant, limit=limit, after=after)
        return {"files": rows, "count": len(rows), "next": next_cursor}

    async def delete(self, file_id: str) -> dict[str, Any]:
        f = await self.get(file_id)
        await self._repo.delete(file_id)
        remaining = await self._repo.decr_ref(f.sha256)
        blob_removed = False
        # Delete the blob ONLY under an atomic claim: `claim_blob_delete` removes the ref row iff it
        # is still zero. If a concurrent upload re-referenced this content between our decrement and
        # here, the claim fails and we leave the bytes — that owner's file must not vanish. Because
        # upload writes the blob before it increments, a re-upload has already restored the bytes.
        if remaining == 0 and await self._repo.claim_blob_delete(f.sha256):
            await self._store.delete(f.sha256)
            blob_removed = True
        return {"deleted": True, "blob_removed": blob_removed}

    async def download_redirect(self, f: FileObject, *, ttl: int | None = None) -> str | None:
        """A presigned bucket URL to serve `f` directly, or None if the store cannot presign.

        Works for any size: on an s3 store even a small file is served straight from the bucket, so
        the app is never in the data path. The caller has already authorized the read; this URL is
        short-lived and scoped to this one object.
        """
        if not getattr(self._store, "supports_presign", False):
            return None
        # Use the configured PRESIGN_TTL_SECONDS, not a hardcoded 300 that ignored the knob (file-r2-07).
        return await self._store.presign_get(f.sha256, f.filename,
                                             ttl if ttl is not None else self._presign_ttl)

    # ── signed URLs ────────────────────────────────────────────────────────────
    def signed_token(self, file_id: str) -> str:
        return self._signed.sign(file_id)

    async def resolve_signed_file(self, token: str) -> FileObject | None:
        payload = self._signed.verify(token)
        if not payload or "key" not in payload:
            return None
        return await self._repo.get(payload["key"])

    async def resolve_signed(self, token: str) -> tuple[FileObject, bytes] | None:
        f = await self.resolve_signed_file(token)
        if f is None:
            return None
        return f, await self._store.get(f.sha256)
