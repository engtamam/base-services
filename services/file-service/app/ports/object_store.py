"""ObjectStore port — blob storage (files, media).

Two models live behind this one port (Phase 34):

  * the SMALL path streams bytes through the app: put()/get()/delete(). Every adapter supports it.
  * the LARGE path keeps the bytes OUT of the app: the client uploads straight to the bucket via a
    presigned multipart plan, and downloads via a presigned GET. Only an adapter that can mint
    presigned URLs supports it, so it declares `supports_presign`. A caller negotiates on that flag
    BEFORE it asks; asking an adapter that cannot presign raises `Unsupported`, never a silent fall
    back to buffering a multi-GB object in the app.

`begin_multipart`/`complete_multipart`/`abort_multipart` bracket a direct-to-bucket upload; `head`
confirms the object landed (existence + size); `stream` reads it back in bounded chunks so the async
scanner can verify and scan a large object without ever holding it whole.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Protocol, runtime_checkable


@dataclass(frozen=True)
class PartUrl:
    part_number: int
    url: str


@dataclass(frozen=True)
class MultipartPlan:
    """A presigned plan the client uses to upload one object directly to the bucket."""
    upload_id: str
    key: str
    part_size: int
    part_count: int
    urls: list[PartUrl]


@dataclass(frozen=True)
class CompletedPart:
    part_number: int
    etag: str


@dataclass(frozen=True)
class ObjectHead:
    """What the store knows about a stored object without downloading it."""
    size: int
    etag: str


@runtime_checkable
class ObjectStore(Protocol):
    driver: str
    # Whether this adapter can mint presigned URLs (the LARGE, direct-to-bucket path). Adapters that
    # cannot (memory, local) set this False; the caller negotiates on it and never reaches the
    # presign methods below on a store that would only raise.
    supports_presign: bool

    # ── small path (every adapter) ────────────────────────────────────────────
    async def put(self, key: str, data: bytes, content_type: str | None = None) -> None: ...

    async def get(self, key: str) -> bytes:
        """Return the object bytes, or raise NotFound."""
        ...

    async def delete(self, key: str) -> bool: ...

    async def exists(self, key: str) -> bool: ...

    async def health(self) -> bool: ...

    async def close(self) -> None: ...

    # ── large path (presign-capable adapters only) ────────────────────────────
    async def begin_multipart(self, key: str, content_type: str, size: int,
                              part_size: int) -> MultipartPlan:
        """Create a multipart upload and return a presigned URL per part. Raise Unsupported if the
        adapter cannot presign."""
        ...

    async def complete_multipart(self, key: str, upload_id: str,
                                 parts: list[CompletedPart]) -> None:
        """Assemble the uploaded parts into the final object."""
        ...

    async def abort_multipart(self, key: str, upload_id: str) -> None:
        """Discard an incomplete multipart upload so its parts stop costing storage."""
        ...

    async def presign_get(self, key: str, filename: str, expires_in: int) -> str:
        """A short-lived URL that serves the object's bytes straight from the bucket."""
        ...

    async def head(self, key: str) -> ObjectHead | None:
        """Object metadata (size, etag) without downloading it, or None if absent."""
        ...

    async def promote(self, src_key: str, dst_key: str) -> None:
        """Server-side move of a verified object from its staging key to its content key.

        A large upload lands on a per-session STAGING key; only after its bytes are hashed and
        scanned is it promoted to the content-addressed key. That keeps an unverified upload from
        ever overwriting a shared content blob (a client that declared a victim's hash would
        otherwise corrupt it). If the content key already exists (dedup), the copy is skipped — the
        existing blob is authoritative by hash — and staging is discarded either way.
        """
        ...

    def stream(self, key: str, chunk_size: int = 1_048_576) -> AsyncIterator[bytes]:
        """Yield the object's bytes in bounded chunks (for scanning/hashing a large object)."""
        ...
