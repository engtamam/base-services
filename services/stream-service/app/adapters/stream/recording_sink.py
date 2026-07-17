"""RecordingSink adapters — where a recorded segment's bytes go.

`memory` (tests) and `local` (a dir) are zero-infra. `file-service` (Phase 35.7) posts the segment to
file-service, reusing its scanned, content-addressed, retained large-object storage — the sink here
holds only the returned object id. Bytes never linger in this service.
"""
from __future__ import annotations

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

    Reuses the ObjectStore adapter. Presigned segment DOWNLOAD needs the object store's presign (or
    the file-service sink, which already presigns); until a segment-download endpoint exists, only
    put/delete are exercised — a recording's bytes are stored durably and reachable by key.
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
        # Presigned download belongs to the object store / file-service sink; not wired until a
        # segment-download endpoint exists.
        raise NotImplementedError("s3 recording sink has no presigned download yet — use file-service")


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
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return SegmentRef(ref=ref, bytes=len(data), start=start, end=end)

    async def delete(self, ref: str) -> bool:
        try:
            os.remove(self._path(ref))
            return True
        except FileNotFoundError:
            return False

    async def signed_url(self, ref: str, *, ttl: int) -> str:
        return f"file://{self._path(ref)}"
