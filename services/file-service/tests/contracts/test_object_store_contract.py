"""ObjectStore contract — memory + local (zero-infra), and s3 against a REAL server.

The s3 adapter had NO param here, in any of the eighteen services. So the contract only ever
proved that the in-memory and local adapters agree with each other — while the driver that
actually holds customer files in production was never held to it at all.

It is the same trap as the postgres contract: **the gate fails on a SKIPPED param, and a param
that does not exist is never skipped.** An absent test does not report itself.

MinIO speaks the S3 API, so it exercises the same adapter a production S3 or a Saudi cloud
S3-compatible store would use. No separate "minio driver" exists, and none should:

    docker run -d -p 9000:9000 -e MINIO_ROOT_USER=minioadmin       -e MINIO_ROOT_PASSWORD=minioadmin minio/minio server /data

    OBJECTSTORE_TEST_ENDPOINT=http://127.0.0.1:9000     OBJECTSTORE_TEST_BUCKET=contract     AWS_ACCESS_KEY_ID=minioadmin AWS_SECRET_ACCESS_KEY=minioadmin     AWS_DEFAULT_REGION=us-east-1 pytest tests/contracts
"""
from __future__ import annotations

import os

import pytest

from app.adapters.object_store.local import LocalObjectStore
from app.adapters.object_store.memory import MemoryObjectStore
from app.ports.errors import NotFound, Unsupported
from tests.contracts.util import run

_S3_ENDPOINT = os.environ.get("OBJECTSTORE_TEST_ENDPOINT", "")
_S3_BUCKET = os.environ.get("OBJECTSTORE_TEST_BUCKET", "contract")


def _s3():
    from app.adapters.object_store.s3 import S3ObjectStore

    # MinIO (and most on-prem/S3-compatible stores) serve PATH-STYLE; virtual-host presigned URLs
    # need a wildcard DNS record and 403 without one. This is the exact footgun the adapter docstring
    # calls out — a presigned multipart PUT is where it bites, so the contract exercises path-style.
    store = S3ObjectStore(_S3_BUCKET, _S3_ENDPOINT, force_path_style=True)
    _ensure_bucket()
    return store


def _ensure_bucket() -> None:
    """Create the bucket if it is not there. A contract run must not depend on hand-made state."""
    import boto3  # provided by aioboto3 (the s3 extra)

    c = boto3.client("s3", endpoint_url=_S3_ENDPOINT)
    try:
        c.head_bucket(Bucket=_S3_BUCKET)
    except Exception:
        c.create_bucket(Bucket=_S3_BUCKET)


@pytest.fixture(params=["memory", "local", "s3"])
def store(request, tmp_path):
    if request.param == "s3":
        if not _S3_ENDPOINT:
            pytest.skip("OBJECTSTORE_TEST_ENDPOINT not set — no S3-compatible server to test")
        s = _s3()
    elif request.param == "local":
        s = LocalObjectStore(str(tmp_path))
    else:
        s = MemoryObjectStore()
    yield s
    run(s.close())


def test_put_get_roundtrip(store):
    run(store.put("dir/file.txt", b"hello"))
    assert run(store.get("dir/file.txt")) == b"hello"


def test_exists_and_delete(store):
    run(store.put("a.bin", b"x"))
    assert run(store.exists("a.bin")) is True
    assert run(store.delete("a.bin")) is True
    assert run(store.exists("a.bin")) is False


def test_get_missing_raises_notfound(store):
    with pytest.raises(NotFound):
        run(store.get("missing.bin"))


def test_delete_missing_is_false_not_an_error(store):
    assert run(store.delete("never-existed.bin")) is False


def test_binary_survives_a_roundtrip(store):
    """Files are not text. A store that mangles bytes corrupts every upload it holds."""
    blob = bytes(range(256))
    run(store.put("raw.bin", blob))
    assert run(store.get("raw.bin")) == blob


def test_overwrite_replaces_the_object(store):
    run(store.put("k", b"first"))
    run(store.put("k", b"second"))
    assert run(store.get("k")) == b"second"


def test_health(store):
    assert run(store.health()) is True


def test_stream_reassembles_the_object(store):
    """The scanner reads a large object back in chunks; the chunks must reassemble byte-for-byte."""
    blob = bytes(range(256)) * 40  # 10 KiB, multi-chunk at a small chunk size
    run(store.put("streamed.bin", blob))

    async def _collect():
        out = bytearray()
        async for chunk in store.stream("streamed.bin", chunk_size=1024):
            out.extend(chunk)
        return bytes(out)

    assert run(_collect()) == blob


def test_head_reports_size_or_none(store):
    run(store.put("sized.bin", b"1234567890"))
    head = run(store.head("sized.bin"))
    assert head is not None and head.size == 10
    assert run(store.head("nope.bin")) is None


def test_promote_moves_staging_to_content_and_dedups(store):
    """promote() copies a verified staging object to its content key, then drops staging. If the
    content key already exists (dedup), the copy is skipped and staging is still discarded."""
    run(store.put("staging/sess1", b"verified-bytes"))
    run(store.promote("staging/sess1", "content-key-aa"))
    assert run(store.get("content-key-aa")) == b"verified-bytes"
    assert run(store.exists("staging/sess1")) is False        # staging released

    # Dedup: a second staging for content that already exists → skip copy, discard staging, keep the
    # authoritative existing blob.
    run(store.put("staging/sess2", b"DIFFERENT-would-corrupt"))
    run(store.promote("staging/sess2", "content-key-aa"))
    assert run(store.get("content-key-aa")) == b"verified-bytes"   # NOT overwritten
    assert run(store.exists("staging/sess2")) is False


def test_presign_capability_is_declared(store):
    """Every adapter states whether it can presign — the caller negotiates on this, never guesses."""
    assert isinstance(store.supports_presign, bool)


def test_presign_on_a_non_presign_store_is_a_clear_refusal(store):
    """A store that cannot presign raises Unsupported — never a silent fall back to buffering."""
    if store.supports_presign:
        pytest.skip("this adapter supports presign; refusal is asserted only for those that do not")
    with pytest.raises(Unsupported):
        run(store.begin_multipart("k", "application/octet-stream", 10, 8))
    with pytest.raises(Unsupported):
        run(store.presign_get("k", "f.bin", 60))


def test_s3_multipart_roundtrip_direct_to_bucket(store):
    """On a real S3, a presigned multipart plan uploads direct-to-bucket and reassembles.

    Proves the LARGE path end to end without the bytes going through any app: presign parts →
    PUT each to the returned URL → complete → head/get see the whole object.
    """
    if not store.supports_presign:
        pytest.skip("presign path is exercised only against a real S3-compatible server")
    import urllib.request

    key = "large/" + "a1" * 32  # a sha256-shaped key
    part_size = 5 * 1024 * 1024  # S3 requires >=5 MiB for non-final parts
    body = b"Z" * (part_size + 1024)  # two parts
    plan = run(store.begin_multipart(key, "application/octet-stream", len(body), part_size))
    assert plan.part_count == 2 and len(plan.urls) == 2

    from app.ports.object_store import CompletedPart
    completed = []
    for pu in plan.urls:
        lo = (pu.part_number - 1) * part_size
        chunk = body[lo:lo + part_size]
        req = urllib.request.Request(pu.url, data=chunk, method="PUT")
        with urllib.request.urlopen(req) as resp:  # noqa: S310 — presigned URL from our own store
            etag = resp.headers.get("ETag", "").strip('"')
        completed.append(CompletedPart(part_number=pu.part_number, etag=etag))

    run(store.complete_multipart(key, plan.upload_id, completed))
    head = run(store.head(key))
    assert head is not None and head.size == len(body)
    assert run(store.get(key)) == body
    run(store.delete(key))


def test_s3_abort_multipart_is_idempotent(store):
    if not store.supports_presign:
        pytest.skip("presign path is exercised only against a real S3-compatible server")
    key = "large/" + "b2" * 32
    plan = run(store.begin_multipart(key, "application/octet-stream", 10, 8))
    run(store.abort_multipart(key, plan.upload_id))
    run(store.abort_multipart(key, plan.upload_id))  # second abort must not raise
