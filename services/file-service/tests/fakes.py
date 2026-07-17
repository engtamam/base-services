"""Test doubles. FakePresignStore lets the LARGE path run without a real S3.

It is a MemoryObjectStore that ADMITS to presigning: `begin_multipart` hands back pretend part URLs,
and a test simulates the client's direct-to-bucket upload by calling `.put(session.staging_key, data)`
itself. head/stream/promote/delete are the real MemoryObjectStore implementations, so the state
transitions (stage → verify → promote → ref-count) are exercised for real. The genuine S3 adapter is
covered separately by the object-store contract against MinIO.
"""
from __future__ import annotations

from app.adapters.object_store.memory import MemoryObjectStore
from app.core.upload_session import new_id
from app.ports.object_store import MultipartPlan, PartUrl


class FakePresignStore(MemoryObjectStore):
    driver = "fake-presign"
    supports_presign = True

    def __init__(self) -> None:
        super().__init__()
        self._multiparts: dict[str, str] = {}

    async def begin_multipart(self, key, content_type, size, part_size):
        upload_id = "mpu-" + new_id()
        part_count = max(1, (size + part_size - 1) // max(1, part_size))
        self._multiparts[upload_id] = key
        urls = [PartUrl(part_number=n, url=f"memory://{key}?part={n}&u={upload_id}")
                for n in range(1, part_count + 1)]
        return MultipartPlan(upload_id=upload_id, key=key, part_size=part_size,
                             part_count=part_count, urls=urls)

    async def complete_multipart(self, key, upload_id, parts):
        # The test already put the bytes at `key` to simulate the direct-to-bucket upload.
        self._multiparts.pop(upload_id, None)

    async def abort_multipart(self, key, upload_id):
        self._multiparts.pop(upload_id, None)

    async def presign_get(self, key, filename, expires_in):
        return f"memory://{key}?filename={filename}&exp={expires_in}"
