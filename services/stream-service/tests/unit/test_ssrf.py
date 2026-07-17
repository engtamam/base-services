"""SSRF guard for source URLs (CWE-918)."""
from __future__ import annotations

import pytest

from app.core.ssrf import SsrfBlocked, validate_source_url


def _res(ips):
    return lambda host: ips


def test_public_host_passes():
    r = validate_source_url("rtsp://cam.example.com:554/s", resolver=_res(["93.184.216.34"]))
    assert r.ip == "93.184.216.34" and r.port == 554 and r.scheme == "rtsp"


def test_metadata_ip_always_blocked_even_with_allow_private():
    with pytest.raises(SsrfBlocked, match="blocked"):
        validate_source_url("http://metadata/latest", allow_private=True,
                            resolver=_res(["169.254.169.254"]))


def test_private_blocked_by_default_allowed_when_configured():
    with pytest.raises(SsrfBlocked, match="private"):
        validate_source_url("rtsp://nvr/s", resolver=_res(["192.168.1.50"]))
    r = validate_source_url("rtsp://nvr/s", allow_private=True, resolver=_res(["192.168.1.50"]))
    assert r.ip == "192.168.1.50"


def test_loopback_and_linklocal_blocked_even_when_private_allowed():
    for ip in ("127.0.0.1", "169.254.10.10"):
        with pytest.raises(SsrfBlocked):
            validate_source_url("rtsp://x/s", allow_private=True, resolver=_res([ip]))


def test_bad_scheme_and_no_host():
    with pytest.raises(SsrfBlocked, match="scheme"):
        validate_source_url("file:///etc/passwd", resolver=_res(["1.2.3.4"]))
    with pytest.raises(SsrfBlocked, match="host"):
        validate_source_url("rtsp://", resolver=_res([]))


def test_allowlist_enforced():
    with pytest.raises(SsrfBlocked, match="ALLOWED_HOSTS"):
        validate_source_url("rtsp://evil.com/s", allowed_hosts=frozenset({"cam.example.com"}),
                            resolver=_res(["93.184.216.34"]))
    r = validate_source_url("rtsp://cam.example.com/s", allowed_hosts=frozenset({"cam.example.com"}),
                            resolver=_res(["93.184.216.34"]))
    assert r.host == "cam.example.com"


def test_multiple_ips_all_must_be_public():
    # DNS returns one public + one private → refuse (a rebinding trick can't sneak the private one).
    with pytest.raises(SsrfBlocked):
        validate_source_url("rtsp://cam/s", resolver=_res(["93.184.216.34", "10.0.0.5"]))
