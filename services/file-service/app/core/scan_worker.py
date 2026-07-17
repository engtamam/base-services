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
                 chunk_size: int = 1_048_576) -> None:
        self._files = files
        self._sessions = sessions
        self._store = object_store
        self._scanner = scanner
        self._queue = queue
        self._scan_queue = scan_queue
        self._policy = scan_policy
        self._chunk = chunk_size

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

    async def run_once(self) -> int:
        """Drain the scan queue once (invoked by a CronJob / a loop). Returns how many it processed."""
        n = 0
        while True:
            job = await self._queue.dequeue(self._scan_queue)
            if job is None:
                break
            sid = (job.payload or {}).get("session_id")
            if sid:
                await self.process(sid)
                n += 1
        return n
