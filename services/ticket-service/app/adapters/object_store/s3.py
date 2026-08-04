"""S3 / S3-compatible ObjectStore — infra-gated, lazy import."""
from __future__ import annotations

from typing import Any

from app.adapters.lazy import load
from app.adapters.object_store.keys import validate_key
from app.ports.errors import ConfigError, NotFound


class S3ObjectStore:
    """Any S3-compatible store: AWS S3, MinIO, or a Saudi cloud's object storage.

    There is no separate "minio driver" and there should not be — MinIO speaks the S3 API, so it
    IS this adapter. What it needs is config, and three knobs were missing:

      * credentials — the adapter relied on the ambient AWS chain (AWS_ACCESS_KEY_ID in the pod's
        environment). That works on AWS and nowhere else. A user pointing us at their own MinIO had
        no supported way to give us its keys.
      * region — boto refuses to sign without one, and MinIO does not care what it is.
      * PATH-STYLE ADDRESSING — and this is the one that actually bites. Boto defaults to
        virtual-host style (`https://bucket.host/key`), which requires a wildcard DNS record. MinIO
        and most on-prem S3 stores serve path-style (`https://host/bucket/key`) and simply fail to
        resolve otherwise. Every "why can't I connect to MinIO" is this.
    """
    driver = "s3"

    def __init__(self, bucket: str, endpoint_url: str = "", *,
                 access_key: str = "", secret_key: str = "", region: str = "",
                 force_path_style: bool = False) -> None:
        if not bucket:
            raise ConfigError("OBJECTSTORE_BUCKET is required for the s3 driver")
        self._aioboto3 = load("aioboto3", "s3")
        self._bucket = bucket
        # Endpoint (if any) comes from config only — never from a caller (SSRF, CWE-918).
        self._endpoint = endpoint_url or None
        self._region = region or None
        self._config = None
        if force_path_style:
            from botocore.config import Config

            self._config = Config(s3={"addressing_style": "path"})
        # Empty credentials => fall back to the ambient chain (IAM role, instance profile). That is
        # the RIGHT default on AWS, where handing us static keys would be worse than not.
        self._creds: dict[str, str] = {}
        if access_key and secret_key:
            self._creds = {"aws_access_key_id": access_key,
                           "aws_secret_access_key": secret_key}
        elif access_key or secret_key:
            raise ConfigError(
                "OBJECTSTORE_ACCESS_KEY and OBJECTSTORE_SECRET_KEY must be set together — "
                "half a credential authenticates nothing, and silently falling back to the "
                "ambient AWS chain would hide the misconfiguration until the first upload")
        self._session = self._aioboto3.Session()

    def _client(self) -> Any:
        return self._session.client("s3", endpoint_url=self._endpoint,
                                    region_name=self._region, config=self._config,
                                    **self._creds)

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
        """True if the object WAS there and is now gone; False if it never existed.

        This returned an unconditional True. S3's `delete_object` is idempotent — it answers 200
        for a key that never existed — so the adapter reported "deleted" for objects it had never
        held, while the memory and local adapters correctly reported False. The same method meant
        "was it there?" in two adapters and "did I send the request?" in the third.

        That is not cosmetic. An erasure receipt built on this claims it deleted a person's file
        when there was no file — a compliance answer that is confidently wrong. The bug survived
        because the s3 adapter had no param in the ObjectStore contract, so nothing ever compared
        the two.
        """
        k = validate_key(key)
        async with self._client() as s3:
            try:
                await s3.head_object(Bucket=self._bucket, Key=k)
            except Exception:
                return False
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
