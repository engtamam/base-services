"""SSRF guard (CWE-918) — the #1 vulnerability of any webhook system.

Customers register arbitrary URLs and *we* make the request. Each of these, if allowed,
is a credential leak or an internal-service pivot.
"""
from __future__ import annotations

import pytest

from app.core.ssrf import SsrfBlocked, validate

# Every one of these must be blocked even with require_https=False (defence in depth —
# the scheme check must never be the ONLY thing standing between us and metadata).
ATTACKS = [
    ("http://169.254.169.254/latest/meta-data/iam/", "aws metadata"),
    ("https://169.254.169.254/", "aws metadata over tls"),
    ("http://[fd00:ec2::254]/", "aws ipv6 metadata"),
    ("http://100.100.100.200/", "alibaba metadata"),
    ("http://192.0.0.192/", "oracle metadata"),
    ("http://127.0.0.1:6379/", "loopback redis"),
    ("http://localhost/hook", "localhost"),
    ("http://10.0.0.5/", "private 10/8"),
    ("http://192.168.1.1/", "private 192.168/16"),
    ("http://172.16.0.1/", "private 172.16/12"),
    ("http://[::1]/", "ipv6 loopback"),
    ("http://[fd00::1]/", "ipv6 unique-local"),
    ("http://[fe80::1]/", "ipv6 link-local"),
    ("http://0.0.0.0/", "unspecified"),
]


@pytest.mark.parametrize("url,label", ATTACKS, ids=[a[1] for a in ATTACKS])
def test_ssrf_targets_are_blocked_even_without_https_requirement(url, label):
    with pytest.raises(SsrfBlocked):
        validate(url, require_https=False)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://x/_", "redis://127.0.0.1:6379",
                                 "ftp://example.com/", "javascript:alert(1)"])
def test_dangerous_schemes_blocked(url):
    with pytest.raises(SsrfBlocked):
        validate(url, require_https=False)


def test_metadata_blocked_even_when_private_ips_allowed():
    """`allow_private_ips` is a dev switch — it must NEVER unlock cloud metadata."""
    for url in ("http://169.254.169.254/", "http://[fd00:ec2::254]/"):
        with pytest.raises(SsrfBlocked, match="metadata"):
            validate(url, require_https=False, allow_private=True)


def test_https_required_by_default():
    with pytest.raises(SsrfBlocked, match="https"):
        validate("http://example.com/hook")


def test_credentials_in_url_blocked():
    with pytest.raises(SsrfBlocked, match="credentials"):
        validate("https://user:pass@example.com/hook")


def test_missing_host_blocked():
    with pytest.raises(SsrfBlocked):
        validate("https:///no-host")


def test_unresolvable_host_blocked():
    with pytest.raises(SsrfBlocked, match="resolve"):
        validate("https://this-host-does-not-exist.invalid/hook")


def test_allow_and_deny_lists():
    with pytest.raises(SsrfBlocked, match="denied"):
        validate("https://example.com/h", denied_hosts=frozenset({"example.com"}))
    with pytest.raises(SsrfBlocked, match="allow-listed"):
        validate("https://example.com/h", allowed_hosts=frozenset({"other.com"}))


def test_public_host_is_allowed_and_pinned():
    target = validate("https://example.com/hook")
    assert target.host == "example.com" and target.port == 443
    assert target.ip and target.ip != "example.com"      # a resolved IP, not the name


def test_private_allowed_only_when_explicitly_enabled():
    target = validate("http://127.0.0.1/h", require_https=False, allow_private=True)
    assert target.ip == "127.0.0.1"
