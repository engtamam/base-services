"""ScanWorker — the async half of a LARGE upload: hash + scan a staged object, then finalize.

A small upload is scanned in the request. A large one cannot be — it is multi-GB and lives in the
bucket — so a background worker does it: stream the staged object ONCE, computing its sha256 as it
goes (integrity) and feeding the same stream to the scanner (safety), then hand the verdict back to
`FileService.finalize_large_upload`, which promotes-or-quarantines. The object is never held whole.

The single pass is the point: hashing and scanning share one read of the bytes. If the scanner
rejects, we quarantine on `rejected` without caring about the hash; if it is clean, the hash we
accumulated is the object's true content hash, checked against the key the client declared.
"""
from __future__ import annotations

import hashlib
from typing import Any

from app.core.file_service import FileService
from app.core.upload_session import SCANNING, UploadSessionStore
from app.ports.scanner import Rejected


class ScanWorker:
    def __init__(self, *, files: FileService, sessions: UploadSessionStore, object_store, scanner,
                 queue: Any, scan_queue: str = "file-scan", scan_policy: str = "async",
                 chunk_size: int = 1_048_576, lease_seconds: int, auditor=None) -> None:
        self._files = files
        self._sessions = sessions
        self._store = object_store
        self._scanner = scanner
        self._queue = queue
        self._scan_queue = scan_queue
        self._policy = scan_policy
        self._chunk = chunk_size
        # Optional. A scan VERDICT — clean or rejected — is exactly what a safety audit turns on, so
        # it is recorded here, per file, where the verdict is known.
        self._auditor = auditor
        # How long a scan may run before another worker may take the job. Config only:
        # a number chosen here would be a guess about how long scanning someone else's
        # file takes, and guessing short means reclaiming work that is still running.
        self._lease_seconds = lease_seconds

    async def process(self, session_id: str) -> None:
        """Verify and scan one staged object, then finalize its session."""
        sess = await self._sessions.get(session_id)
        if sess is None or sess.status != SCANNING:
            return   # already handled, or not ready — idempotent

        hasher = hashlib.sha256()

        async def _tee():
            # One read of the bytes feeds BOTH the hasher (side effect) and the scanner (consumer).
            async for chunk in self._store.stream(sess.staging_key, self._chunk):
                hasher.update(chunk)
                yield chunk

        clean, reason = True, ""
        if self._policy == "skip":
            # Verify integrity only — drain to hash, do not scan. (Refused in production by the
            # boot guard; a dev convenience.)
            async for _ in _tee():
                pass
        else:
            try:
                await self._scanner.scan_stream(_tee(), sess.content_type)
            except Rejected as exc:
                clean, reason = False, str(exc) or "rejected"

        computed = hasher.hexdigest() if clean else ""
        await self._files.finalize_large_upload(session_id, computed_sha=computed,
                                                 clean=clean, reason=reason)
        if self._auditor is not None:
            # Non-critical (a scanner outage must not wedge the queue), actor "system" — a CronJob
            # drives this. A rejected verdict is the one a security review reads first.
            await self._auditor.record(
                "file.scanned", actor="system", tenant=getattr(sess, "tenant", ""),
                resource=f"upload:{session_id}",
                metadata={"clean": str(clean), "reason": reason})

    async def run_once(self) -> int:
        """Drain the scan queue once (invoked by a CronJob / a loop). Returns how many it processed.

        LEASED, NOT POPPED. This loop used to take a job off the queue and then scan it, so a
        worker killed mid-scan — an OOM on a large upload, a node drain, a rolling deploy — took
        the job with it. The upload stayed in SCANNING for ever, nothing retried it, and nothing
        anywhere recorded that the work had existed.

        Now the job is only deleted once `process` returns. A crash before that leaves the lease
        to expire and the job returns by itself. `process` is idempotent — it re-reads the session
        and does nothing unless the status is still SCANNING — so the duplicate a lease can
        produce is a no-op, which is what makes at-least-once safe to accept here.
        """
        # Anything a dead worker left behind comes back first. Without this the queue looks empty
        # while jobs sit in flight with nobody holding them.
        await self._queue.reclaim(self._scan_queue)

        n = failed = 0
        while True:
            job = await self._queue.dequeue(self._scan_queue, lease_seconds=self._lease_seconds)
            if job is None:
                break
            sid = (job.payload or {}).get("session_id")
            if not sid:
                # Malformed and unprocessable: acked rather than nacked, because returning it
                # would retry it for ever and block nothing but itself.
                await self._queue.ack(job)
                continue
            try:
                await self.process(sid)
            except Exception as exc:
                # file-012: nack THIS job and keep draining — one poison object must not stall the rest
                # of the batch for the whole invocation. Handing it back (not acking) retries it promptly
                # via the lease; the scan is not marked done, so nothing downstream sees a false verdict.
                # The failure is recorded so a repeatedly-failing object is visible (a dead-letter/attempt
                # counter on the queue is the follow-up for permanent poison).
                await self._queue.nack(job)
                failed += 1
                if self._auditor is not None:
                    try:
                        await self._auditor.record(
                            "file.scan_failed", actor="system",
                            resource=f"upload:{sid}",
                            metadata={"error": type(exc).__name__})
                    except Exception:
                        pass
                continue
            await self._queue.ack(job)
            n += 1
        return n
