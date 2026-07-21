"""Local filesystem ObjectStore — real, zero infra. Path-traversal-safe (CWE-22)."""
from __future__ import annotations

import asyncio
import os

from app.adapters.object_store.keys import validate_key
from app.ports.errors import NotFound, Unsupported


class LocalObjectStore:
    driver = "local"
    supports_presign = False   # a local dir is not a bucket a client could reach

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

    async def presign_get(self, key: str, expires_in: int) -> str:
        raise Unsupported("local object store cannot presign — use an s3 store for segment download")

    async def health(self) -> bool:
        return await asyncio.to_thread(os.path.isdir, self._root)

    async def close(self) -> None:
        return None
