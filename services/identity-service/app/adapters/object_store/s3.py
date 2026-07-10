"""S3 / S3-compatible ObjectStore — infra-gated, lazy import."""
from __future__ import annotations

from typing import Any

from app.adapters.lazy import load
from app.adapters.object_store.keys import validate_key
from app.ports.errors import ConfigError, NotFound


class S3ObjectStore:
    driver = "s3"

    def __init__(self, bucket: str, endpoint_url: str = "") -> None:
        if not bucket:
            raise ConfigError("OBJECTSTORE_BUCKET is required for the s3 driver")
        self._aioboto3 = load("aioboto3", "s3")
        self._bucket = bucket
        # Endpoint (if any) comes from config only — never from a caller (SSRF, CWE-918).
        self._endpoint = endpoint_url or None
        self._session = self._aioboto3.Session()

    def _client(self) -> Any:
        return self._session.client("s3", endpoint_url=self._endpoint)

    async def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        k = validate_key(key)
        extra = {"ContentType": content_type} if content_type else {}
        async with self._client() as s3:
            await s3.put_object(Bucket=self._bucket, Key=k, Body=bytes(data), **extra)

    async def get(self, key: str) -> bytes:
        k = validate_key(key)
        async with self._client() as s3:
            try:
                resp = await s3.get_object(Bucket=self._bucket, Key=k)
                async with resp["Body"] as stream:
                    return await stream.read()
            except s3.exceptions.NoSuchKey as exc:
                raise NotFound(f"object not found: {k}") from exc

    async def delete(self, key: str) -> bool:
        k = validate_key(key)
        async with self._client() as s3:
            await s3.delete_object(Bucket=self._bucket, Key=k)
        return True

    async def exists(self, key: str) -> bool:
        k = validate_key(key)
        async with self._client() as s3:
            try:
                await s3.head_object(Bucket=self._bucket, Key=k)
                return True
            except Exception:
                return False

    async def health(self) -> bool:
        try:
            async with self._client() as s3:
                await s3.head_bucket(Bucket=self._bucket)
            return True
        except Exception:
            return False

    async def close(self) -> None:
        return None
