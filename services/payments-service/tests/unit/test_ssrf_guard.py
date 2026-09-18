"""H1 — SSRF guard for outbound gateway fetches + write-time MERCHANT_API_BASE validation."""
from __future__ import annotations

from app.adapters.payment_http import guard_fetch_url, validate_gateway_base


def test_fetch_guard_blocks_metadata_loopback_and_private():
    assert guard_fetch_url("http://169.254.169.254/latest/meta-data/") is not None   # cloud metadata
    assert guard_fetch_url("http://127.0.0.1:8080/") is not None                      # loopback
    assert guard_fetch_url("https://127.0.0.1/") is not None
    assert guard_fetch_url("http://[::1]/") is not None                               # ipv6 loopback
    assert guard_fetch_url("http://10.0.0.5/") is not None                            # private
    assert guard_fetch_url("http://192.168.1.1/") is not None                         # private


def test_fetch_guard_blocks_non_web_schemes():
    assert guard_fetch_url("file:///etc/passwd") is not None
    assert guard_fetch_url("ftp://evil.example/x") is not None
    assert guard_fetch_url("gopher://evil.example/x") is not None


def test_fetch_guard_blocks_missing_or_unresolvable_host():
    assert guard_fetch_url("https:///nohost") is not None
    assert guard_fetch_url("https://this-host-does-not-exist.invalid/") is not None


def test_fetch_guard_allows_a_public_https_host():
    # A public, resolvable host is allowed (uses real DNS; example.com is stable).
    assert guard_fetch_url("https://example.com/api/v1") is None


def test_write_validation_requires_https_and_public_host():
    ok, _ = validate_gateway_base("https://onboarding-prod.tuwaiqpay.com.sa/api/v1")
    assert ok is True
    assert validate_gateway_base("http://onboarding-prod.tuwaiqpay.com.sa/api/v1")[0] is False  # not https
    assert validate_gateway_base("https://169.254.169.254/")[0] is False                         # metadata IP
    assert validate_gateway_base("https://127.0.0.1/")[0] is False                               # loopback IP
    assert validate_gateway_base("file:///etc/passwd")[0] is False
    assert validate_gateway_base("https:///nohost")[0] is False
