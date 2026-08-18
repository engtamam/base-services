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
