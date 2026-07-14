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
    """THE MinIO knob. Boto defaults to virtual-host addressing (https://bucket.host/key), which
    needs a wildcard DNS record MinIO does not have — so it fails to resolve. Path style
    (https://host/bucket/key) is what on-prem stores serve."""
    virtual = S3ObjectStore("b", "http://minio:9000")
    assert virtual._config is None

    path = S3ObjectStore("b", "http://minio:9000", force_path_style=True)
    assert path._config.s3["addressing_style"] == "path"


def test_minio_style_wiring_is_accepted():
    s = S3ObjectStore("uploads", "http://minio:9000", access_key="minioadmin",
                      secret_key="minioadmin", region="us-east-1", force_path_style=True)
    assert s._endpoint == "http://minio:9000"
    assert s._region == "us-east-1"
    assert s._creds["aws_access_key_id"] == "minioadmin"
