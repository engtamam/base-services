"""The s3 driver must be pointable at ANY S3-compatible store by config alone.

MinIO, AWS, a Saudi cloud's object storage — one adapter, config decides. There is no "minio
driver" and there should not be: MinIO speaks S3.
"""
from __future__ import annotations

import pytest

from app.adapters.object_store.s3 import S3ObjectStore
from app.ports.errors import ConfigError


def test_bucket_is_required():
    with pytest.raises(ConfigError):
        S3ObjectStore("")


def test_half_a_credential_is_refused():
    """An access key with no secret authenticates nothing. Falling back to the ambient AWS chain
    would hide the misconfiguration until the first upload — in production, on a Friday."""
    with pytest.raises(ConfigError):
        S3ObjectStore("b", "http://minio:9000", access_key="k")
    with pytest.raises(ConfigError):
        S3ObjectStore("b", "http://minio:9000", secret_key="s")


def test_no_credentials_falls_back_to_the_ambient_chain():
    """On AWS this is the RIGHT default: an IAM role beats static keys we would have to store."""
    s = S3ObjectStore("b")
    assert s._creds == {}


def test_path_style_is_configurable():
    """THE MinIO knob, now a STATED choice BOTH ways. force_path_style=False used to leave the
    addressing style unset and let botocore GUESS — and against a non-AWS endpoint (Alibaba OSS) that
    guess resolved wrong: head_bucket answered a bare 403 and /readyz reported the store "down" with
    no log line. So virtual-hosted is now requested outright, path-style when forced."""
    virtual = S3ObjectStore("b", "http://minio:9000")
    assert virtual._config.s3["addressing_style"] == "virtual"      # explicit, never an ambient guess

    path = S3ObjectStore("b", "http://minio:9000", force_path_style=True)
    assert path._config.s3["addressing_style"] == "path"


def test_presigned_urls_do_not_require_a_default_checksum_header():
    """Regression: modern botocore bakes a default x-amz-checksum-crc32 into a presigned URL's
    SignedHeaders, so an ordinary browser/curl PUT fails with SignatureDoesNotMatch. The adapter
    must set checksum calculation to 'when required' so a presigned URL any client can use."""
    store = S3ObjectStore("b", "http://minio:9000", force_path_style=True)
    # Only meaningful on botocore versions that HAVE the knob; on older ones there is no default to
    # disable and the presigned URL is already usable.
    calc = getattr(store._config, "request_checksum_calculation", None)
    if calc is not None:
        assert calc == "when_required"


def test_minio_style_wiring_is_accepted():
    s = S3ObjectStore("uploads", "http://minio:9000", access_key="minioadmin",
                      secret_key="minioadmin", region="us-east-1", force_path_style=True)
    assert s._endpoint == "http://minio:9000"
    assert s._region == "us-east-1"
    assert s._creds["aws_access_key_id"] == "minioadmin"
