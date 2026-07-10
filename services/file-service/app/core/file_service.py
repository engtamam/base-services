"""Core file logic — validated upload, content-addressed storage, signed URLs.

Content-addressing: the blob key is sha256(bytes), so identical uploads dedup to
one blob; a ref-count makes delete safe (the blob is removed only when the last
referencing file is deleted).
"""
from __future__ import annotations

import hashlib
from typing import Any

from app.core.file_repository import FileRepository
from app.core.models import FileObject, new_id
from app.ports.errors import NotFound


class TooLarge(Exception):
    pass


class TypeNotAllowed(Exception):
    pass


class FileService:
    def __init__(self, *, repo: FileRepository, object_store, scanner, signed_url,
                 max_upload_bytes: int, allowed_content_types: frozenset[str],
                 max_page_limit: int = 100) -> None:
        self._repo = repo
        self._store = object_store
        self._scanner = scanner
        self._signed = signed_url
        self._max = max_upload_bytes
        self._allowed = allowed_content_types
        self._max_page = max_page_limit

    async def upload(self, *, tenant: str, owner: str, filename: str,
                     content_type: str, data: bytes) -> FileObject:
        if len(data) > self._max:
            raise TooLarge(f"upload exceeds {self._max} bytes")
        ct = (content_type or "application/octet-stream").split(";")[0].strip().lower()
        if self._allowed and ct not in self._allowed:
            raise TypeNotAllowed(ct)
        self._scanner.scan(data, ct)  # raises Rejected
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
        if remaining == 0:
            await self._store.delete(f.sha256)
            blob_removed = True
        return {"deleted": True, "blob_removed": blob_removed}

    # ── signed URLs ────────────────────────────────────────────────────────────
    def signed_token(self, file_id: str) -> str:
        return self._signed.sign(file_id)

    async def resolve_signed(self, token: str) -> tuple[FileObject, bytes] | None:
        payload = self._signed.verify(token)
        if not payload or "key" not in payload:
            return None
        f = await self._repo.get(payload["key"])
        if f is None:
            return None
        return f, await self._store.get(f.sha256)
