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
from app.ports.errors import NotFound
from tests.contracts.util import run

_S3_ENDPOINT = os.environ.get("OBJECTSTORE_TEST_ENDPOINT", "")
_S3_BUCKET = os.environ.get("OBJECTSTORE_TEST_BUCKET", "contract")


def _s3():
    from app.adapters.object_store.s3 import S3ObjectStore

    store = S3ObjectStore(_S3_BUCKET, _S3_ENDPOINT)
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
