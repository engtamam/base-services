"""S3 / S3-compatible ObjectStore — infra-gated, lazy import."""
from __future__ import annotations

from typing import Any, AsyncIterator

from app.adapters.lazy import load
from app.adapters.object_store.keys import validate_key
from app.ports.errors import ConfigError, NotFound
from app.ports.object_store import CompletedPart, MultipartPlan, ObjectHead, PartUrl


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
    supports_presign = True   # S3 can mint presigned URLs → it carries the LARGE, direct-to-bucket path

    def __init__(self, bucket: str, endpoint_url: str = "", *,
                 access_key: str = "", secret_key: str = "", region: str = "",
                 force_path_style: bool = False, presign_ttl: int = 900) -> None:
        if not bucket:
            raise ConfigError("OBJECTSTORE_BUCKET is required for the s3 driver")
        self._aioboto3 = load("aioboto3", "s3")
        self._bucket = bucket
        self._presign_ttl = max(1, int(presign_ttl))
        # Endpoint (if any) comes from config only — never from a caller (SSRF, CWE-918).
        self._endpoint = endpoint_url or None
        self._region = region or None
        from botocore.config import Config

        # PRESIGNED-URL SAFETY. Since botocore 1.36 the SDK adds a default request checksum
        # (x-amz-checksum-crc32) and bakes it into a presigned URL's SignedHeaders — so an ordinary
        # browser/curl PUT that does not replay that exact header fails with SignatureDoesNotMatch.
        # A presigned URL exists precisely so an ARBITRARY client can upload, so we force checksum
        # calculation to "when required" — the presigned URL then signs only `host`, and any client
        # can use it. (Whole-object integrity is verified separately, by hashing the object on the
        # scan pass, not by a per-request header.)
        cfg: dict[str, Any] = {"signature_version": "s3v4"}
        if force_path_style:
            cfg["s3"] = {"addressing_style": "path"}
        try:
            self._config = Config(request_checksum_calculation="when_required",
                                  response_checksum_validation="when_required", **cfg)
        except TypeError:   # botocore < 1.36 has no such knobs (and no such default to disable)
            self._config = Config(**cfg)
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

    # ── large path: presigned, direct-to-bucket multipart ─────────────────────
    async def begin_multipart(self, key: str, content_type: str, size: int,
                              part_size: int) -> MultipartPlan:
        """Create a multipart upload and presign one PUT URL per part.

        The client uploads each part DIRECTLY to the bucket with these URLs — the bytes never touch
        this process. A URL grants writing ONE (bucket, key, upload_id, part) and nothing else, and
        expires with `_presign_ttl`; the key was derived server-side from the declared content hash,
        so a client cannot presign over another object.
        """
        k = validate_key(key)
        if part_size <= 0 or size < 0:
            raise ConfigError("multipart part_size must be > 0 and size >= 0")
        part_count = max(1, (size + part_size - 1) // part_size)
        extra = {"ContentType": content_type} if content_type else {}
        async with self._client() as s3:
            created = await s3.create_multipart_upload(Bucket=self._bucket, Key=k, **extra)
            upload_id = created["UploadId"]
            urls = []
            for n in range(1, part_count + 1):
                url = await s3.generate_presigned_url(
                    "upload_part",
                    Params={"Bucket": self._bucket, "Key": k, "UploadId": upload_id, "PartNumber": n},
                    ExpiresIn=self._presign_ttl)
                urls.append(PartUrl(part_number=n, url=url))
        return MultipartPlan(upload_id=upload_id, key=k, part_size=part_size,
                             part_count=part_count, urls=urls)

    async def complete_multipart(self, key: str, upload_id: str,
                                 parts: list[CompletedPart]) -> None:
        k = validate_key(key)
        payload = {"Parts": [{"ETag": p.etag, "PartNumber": p.part_number}
                             for p in sorted(parts, key=lambda p: p.part_number)]}
        async with self._client() as s3:
            await s3.complete_multipart_upload(Bucket=self._bucket, Key=k,
                                               UploadId=upload_id, MultipartUpload=payload)

    async def abort_multipart(self, key: str, upload_id: str) -> None:
        k = validate_key(key)
        async with self._client() as s3:
            try:
                await s3.abort_multipart_upload(Bucket=self._bucket, Key=k, UploadId=upload_id)
            except Exception:   # noqa: BLE001 — an already-gone upload is already aborted (idempotent)
                pass

    async def presign_get(self, key: str, filename: str, expires_in: int) -> str:
        """A short-lived URL that serves the object straight from the bucket, as an attachment.

        `ResponseContentDisposition` forces a download with the original filename, so a presigned
        GET of a text/html blob cannot be coaxed into rendering inline in the victim's origin.
        """
        k = validate_key(key)
        safe = (filename or "download.bin").replace('"', "").replace("\r", "").replace("\n", "")[:255]
        async with self._client() as s3:
            return await s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": k,
                        "ResponseContentDisposition": f'attachment; filename="{safe}"'},
                ExpiresIn=max(1, int(expires_in)))

    async def head(self, key: str) -> ObjectHead | None:
        k = validate_key(key)
        async with self._client() as s3:
            try:
                resp = await s3.head_object(Bucket=self._bucket, Key=k)
            except Exception:
                return None
        return ObjectHead(size=int(resp.get("ContentLength", 0)),
                          etag=str(resp.get("ETag", "")).strip('"'))

    async def promote(self, src_key: str, dst_key: str) -> None:
        src, dst = validate_key(src_key), validate_key(dst_key)
        async with self._client() as s3:
            # Dedup: if the content key already holds this blob (same hash → same bytes), skip the
            # copy and just drop staging. copy_object handles objects up to 5 GiB — MAX_OBJECT_BYTES.
            try:
                await s3.head_object(Bucket=self._bucket, Key=dst)
                exists = True
            except Exception:
                exists = False
            if not exists:
                await s3.copy_object(Bucket=self._bucket, Key=dst,
                                     CopySource={"Bucket": self._bucket, "Key": src})
            try:
                await s3.delete_object(Bucket=self._bucket, Key=src)
            except Exception:   # noqa: BLE001 — staging already gone is fine
                pass

    async def stream(self, key: str, chunk_size: int = 1_048_576) -> AsyncIterator[bytes]:
        """Read the object back in bounded chunks — the scanner hashes+scans a multi-GB object
        without this process ever holding it whole."""
        k = validate_key(key)
        async with self._client() as s3:
            try:
                resp = await s3.get_object(Bucket=self._bucket, Key=k)
            except s3.exceptions.NoSuchKey as exc:
                raise NotFound(f"object not found: {k}") from exc
            async with resp["Body"] as body:
                # aiobotocore's body wraps an aiohttp stream; its `.read()` takes no size, so chunk
                # via the underlying content reader (falling back to StreamingBody.iter_chunks).
                content = getattr(body, "content", None)
                if content is not None and hasattr(content, "iter_chunked"):
                    async for chunk in content.iter_chunked(chunk_size):
                        yield chunk
                elif hasattr(body, "iter_chunks"):
                    async for chunk in body.iter_chunks(chunk_size):
                        yield chunk
                else:                                  # last resort: whole-object read
                    yield await body.read()

    async def close(self) -> None:
        return None
