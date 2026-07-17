"""Local filesystem ObjectStore — real, zero infra. Path-traversal-safe (CWE-22)."""
from __future__ import annotations

import asyncio
import os
from typing import AsyncIterator

from app.adapters.object_store.keys import validate_key
from app.ports.errors import NotFound, Unsupported
from app.ports.object_store import CompletedPart, MultipartPlan, ObjectHead


class LocalObjectStore:
    driver = "local"
    supports_presign = False   # a local path is not a bucket the client can reach → SMALL path only

    def __init__(self, root: str) -> None:
        self._root = os.path.abspath(root or "./.objectstore")
        os.makedirs(self._root, exist_ok=True)

    def _path(self, key: str) -> str:
        safe = validate_key(key)
        full = os.path.abspath(os.path.join(self._root, safe))
        # Defense-in-depth: ensure the resolved path stays under root.
        if not (full == self._root or full.startswith(self._root + os.sep)):
            raise NotFound("object key escapes store root")
        return full

    def _write(self, path: str, data: bytes) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)

    async def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        await asyncio.to_thread(self._write, self._path(key), bytes(data))

    async def get(self, key: str) -> bytes:
        path = self._path(key)
        try:
            return await asyncio.to_thread(lambda: open(path, "rb").read())
        except FileNotFoundError as exc:
            raise NotFound(f"object not found: {key}") from exc

    async def delete(self, key: str) -> bool:
        path = self._path(key)
        try:
            await asyncio.to_thread(os.remove, path)
            return True
        except FileNotFoundError:
            return False

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(os.path.isfile, self._path(key))

    async def health(self) -> bool:
        return await asyncio.to_thread(os.path.isdir, self._root)

    # ── large path: not available (a local path is not a client-reachable bucket) ──
    async def begin_multipart(self, key: str, content_type: str, size: int,
                              part_size: int) -> MultipartPlan:
        raise Unsupported("local object store cannot presign — use an s3 store for large uploads")

    async def complete_multipart(self, key: str, upload_id: str,
                                 parts: list[CompletedPart]) -> None:
        raise Unsupported("local object store cannot presign")

    async def abort_multipart(self, key: str, upload_id: str) -> None:
        raise Unsupported("local object store cannot presign")

    async def presign_get(self, key: str, filename: str, expires_in: int) -> str:
        raise Unsupported("local object store cannot presign")

    async def head(self, key: str) -> ObjectHead | None:
        path = self._path(key)
        try:
            size = await asyncio.to_thread(os.path.getsize, path)
        except FileNotFoundError:
            return None
        return ObjectHead(size=size, etag="")

    async def promote(self, src_key: str, dst_key: str) -> None:
        src, dst = self._path(src_key), self._path(dst_key)

        def _move() -> None:
            if not os.path.isfile(dst) and os.path.isfile(src):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                os.replace(src, dst)          # atomic on the same filesystem
            elif os.path.isfile(src):
                os.remove(src)                # dst already exists (dedup) → discard staging

        await asyncio.to_thread(_move)

    async def stream(self, key: str, chunk_size: int = 1_048_576) -> AsyncIterator[bytes]:
        path = self._path(key)
        try:
            fh = await asyncio.to_thread(open, path, "rb")
        except FileNotFoundError as exc:
            raise NotFound(f"object not found: {key}") from exc
        try:
            while True:
                chunk = await asyncio.to_thread(fh.read, max(1, chunk_size))
                if not chunk:
                    break
                yield chunk
        finally:
            await asyncio.to_thread(fh.close)

    async def close(self) -> None:
        return None
