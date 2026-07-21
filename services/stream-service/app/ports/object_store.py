"""ObjectStore port — blob storage (files, media)."""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ObjectStore(Protocol):
    driver: str
    # Whether this driver can mint a presigned GET. S3/MinIO can (the client reaches the bucket
    # directly); a memory or local store cannot, because there is no bucket URL a browser could
    # resolve. The s3 recording sink checks this rather than assuming — the alternative is a
    # NotImplementedError at export time, which is what left "the sink exports or erases, never
    # both" for s3 deployments.
    supports_presign: bool

    async def put(self, key: str, data: bytes, content_type: str | None = None) -> None: ...

    async def get(self, key: str) -> bytes:
        """Return the object bytes, or raise NotFound."""
        ...

    async def delete(self, key: str) -> bool: ...

    async def exists(self, key: str) -> bool: ...

    async def presign_get(self, key: str, expires_in: int) -> str:
        """A short-lived URL that serves the object straight from the store.

        Raises Unsupported on a driver that cannot presign (memory/local). Used by the s3
        recording sink so a segment download is served by the bucket, never streamed through this
        service.
        """
        ...

    async def health(self) -> bool: ...

    async def close(self) -> None: ...
