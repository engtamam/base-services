"""Addressing style is a STATED choice both ways — never botocore's ambient guess.

force_path_style=False used to leave the client Config unset and let botocore GUESS the addressing
style. Against a non-AWS endpoint (Alibaba OSS) that guess resolved wrong: head_bucket answered a bare
403 and /readyz reported the store "down" with no log line to distinguish it from a real outage. The
adapter now requests virtual-hosted addressing outright when path-style is not forced.

Needs the s3 extra (aioboto3/botocore) to construct the client Config; skipped where it is absent.
"""
from __future__ import annotations

import pytest

aioboto3 = pytest.importorskip("aioboto3", reason="s3 extra not installed")

from app.adapters.object_store import credentials as C          # noqa: E402
from app.adapters.object_store.s3 import S3ObjectStore          # noqa: E402

_STATIC = C.CredentialConfig(mode="static", access_key="a", secret_key="b")


@pytest.mark.parametrize("force_path_style,expected", [(False, "virtual"), (True, "path")])
def test_addressing_style_is_explicit_both_ways(force_path_style, expected):
    store = S3ObjectStore("bkt", "https://oss-cn.example.com",
                          force_path_style=force_path_style, credentials=_STATIC)
    assert store._config is not None, "addressing must be a stated Config, never None (the guess)"
    assert store._config.s3["addressing_style"] == expected


def test_upload_checksums_are_computed_only_when_required():
    """botocore >= 1.36 defaults uploads to STREAMING-UNSIGNED-PAYLOAD-TRAILER checksums, which Alibaba
    OSS rejects with NotImplemented — every put_object fails and no off-server backup is written. The
    adapter must request checksums only WHEN REQUIRED so the body streams in a form those stores accept.
    Skipped on botocore < 1.36, which has no such knob (nor the trailer default to disable)."""
    cfg = S3ObjectStore("bkt", "https://oss-cn.example.com", credentials=_STATIC)._config
    if not hasattr(cfg, "request_checksum_calculation"):
        pytest.skip("botocore < 1.36 has no checksum knob and no trailer default")
    assert cfg.request_checksum_calculation == "when_required"
    assert cfg.response_checksum_validation == "when_required"


def test_client_has_bounded_connect_and_read_timeouts():
    """An unreachable/slow store must FAIL FAST, not hang on botocore's ~60s-per-attempt default and
    blow a health probe's deadline (the object_store /readyz check saw exactly that). The adapter
    pins short connect/read timeouts + a bounded retry so a dead endpoint is a clean quick 'down'."""
    cfg = S3ObjectStore("bkt", "https://oss-cn.example.com", credentials=_STATIC)._config
    assert cfg.connect_timeout and cfg.connect_timeout <= 10, "a bounded connect timeout, not the default"
    assert cfg.read_timeout and cfg.read_timeout <= 30
    assert (cfg.retries or {}).get("max_attempts", 99) <= 4      # bounded, never botocore's default storm
