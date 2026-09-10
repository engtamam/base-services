"""RecordingSink adapters — where a recorded segment's bytes go.

`memory` (tests) and `local` (a dir) are zero-infra. `file-service` (Phase 35.7) posts the segment to
file-service, reusing its scanned, content-addressed, retained large-object storage — the sink here
holds only the returned object id. Bytes never linger in this service.
"""
from __future__ import annotations

import asyncio
import os

from app.ports.stream_types import SegmentRef


class MemoryRecordingSink:
    driver = "memory"

    def __init__(self) -> None:
        self.segments: dict[str, bytes] = {}

    async def put_segment(self, *, tenant: str, session_id: str, seq: int, data: bytes,
                          content_type: str, start: str, end: str) -> SegmentRef:
        ref = f"{tenant}/{session_id}/{seq:08d}"
        self.segments[ref] = bytes(data)
        return SegmentRef(ref=ref, bytes=len(data), start=start, end=end)

    async def delete(self, ref: str) -> bool:
        return self.segments.pop(ref, None) is not None

    async def signed_url(self, ref: str, *, ttl: int) -> str:
        return f"memory://{ref}?ttl={ttl}"


class S3RecordingSink:
    """Store segments straight in the object store (S3/MinIO) — durable, no cross-service auth.

    Reuses the ObjectStore adapter, including its presigned GET: a segment download is served by
    the bucket, never streamed through this service. Until this was wired, signed_url raised
    NotImplementedError, so an s3-sink deployment could store and delete footage but could not
    EXPORT it — "the sink exports or erases, never both". It now does both.
    """
    driver = "s3"

    def __init__(self, object_store) -> None:
        self._store = object_store

    def _key(self, tenant: str, session_id: str, seq: int) -> str:
        return f"rec/{tenant}/{session_id}/{seq:08d}.mp4"

    async def put_segment(self, *, tenant: str, session_id: str, seq: int, data: bytes,
                          content_type: str, start: str, end: str) -> SegmentRef:
        key = self._key(tenant, session_id, seq)
        await self._store.put(key, data, content_type)
        return SegmentRef(ref=key, bytes=len(data), start=start, end=end)

    async def delete(self, ref: str) -> bool:
        return await self._store.delete(ref)

    async def signed_url(self, ref: str, *, ttl: int) -> str:
        # The object store presigns; the bytes are served by the bucket, not by us. A store that
        # cannot presign (memory/local) raises Unsupported — which is honest, and only reachable
        # in a misconfigured deployment, because the s3 sink is only ever paired with an s3 store.
        return await self._store.presign_get(ref, expires_in=ttl)


class LocalRecordingSink:
    driver = "local"

    def __init__(self, root: str) -> None:
        self._root = os.path.abspath(root or "./.recordings")
        os.makedirs(self._root, exist_ok=True)

    def _path(self, ref: str) -> str:
        # ref is tenant/session/seq — all safe segments; guard traversal defensively.
        full = os.path.abspath(os.path.join(self._root, ref))
        if not (full == self._root or full.startswith(self._root + os.sep)):
            raise ValueError("recording ref escapes root")
        return full

    async def put_segment(self, *, tenant: str, session_id: str, seq: int, data: bytes,
                          content_type: str, start: str, end: str) -> SegmentRef:
        ref = f"{tenant}/{session_id}/{seq:08d}"
        path = self._path(ref)

        def _write() -> None:   # blocking disk I/O off the event loop (a segment can be large)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(data)

        await asyncio.to_thread(_write)
        return SegmentRef(ref=ref, bytes=len(data), start=start, end=end)

    async def delete(self, ref: str) -> bool:
        try:
            os.remove(self._path(ref))
            return True
        except FileNotFoundError:
            return False

    async def signed_url(self, ref: str, *, ttl: int) -> str:
        return f"file://{self._path(ref)}"
