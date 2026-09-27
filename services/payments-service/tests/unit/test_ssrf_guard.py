"""H1 — SSRF guard for outbound gateway fetches + write-time MERCHANT_API_BASE validation.
FIN-012 — the resolve→connect DNS-rebind window is closed by pinning the validated IP at connect."""
from __future__ import annotations

import socket

import app.adapters.payment_http as ph
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


# ── FIN-012: pin the validated IP at connect — no DNS-rebind ──────────────────────────────────────
def _fake_infos(ip):
    fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return [(fam, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443))]


def test_resolve_pinned_returns_the_validated_public_ip(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _fake_infos("8.8.8.8"))
    reason, ip = ph._resolve_pinned("https://gw.example/api")
    assert reason is None and ip == "8.8.8.8"


def test_resolve_pinned_blocks_when_any_resolved_ip_is_private(monkeypatch):
    # If resolution yields a blocked address, refuse — with no pinned IP to connect to.
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _fake_infos("169.254.169.254"))
    reason, ip = ph._resolve_pinned("https://gw.example/api")
    assert reason is not None and ip is None


def test_send_connects_to_the_pinned_ip_and_never_reresolves(monkeypatch):
    # The rebind attack: DNS answers a PUBLIC ip to the guard, then a PRIVATE ip at connect. We resolve
    # ONCE (public), pin it, and the connection targets THAT ip — a later getaddrinfo is never consulted.
    calls = {"n": 0}

    def flip(*a, **k):
        calls["n"] += 1
        return _fake_infos("8.8.8.8" if calls["n"] == 1 else "169.254.169.254")

    monkeypatch.setattr(socket, "getaddrinfo", flip)

    connected_to = {}

    def fake_create_connection(address, *a, **k):
        connected_to["addr"] = address
        raise OSError("stop before real I/O")     # we only need the target address

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    status, _ = ph._send("https://gw.example/api", b"{}", {"Accept": "application/json"})
    assert status == 0                             # our OSError → unreachable (never a fake decline)
    assert connected_to["addr"][0] == "8.8.8.8"   # the PINNED public ip, not the rebind private one


def test_pinned_https_uses_the_hostname_for_sni_and_cert_not_the_ip():
    # The socket goes to the IP, but TLS (SNI + cert name) + the Host header use the real hostname.
    conn = ph._PinnedHTTPSConnection("gw.example", "8.8.8.8", 443)
    assert conn.host == "gw.example" and conn._ip == "8.8.8.8"


def test_write_validation_requires_https_and_public_host():
    ok, _ = validate_gateway_base("https://onboarding-prod.tuwaiqpay.com.sa/api/v1")
    assert ok is True
    assert validate_gateway_base("http://onboarding-prod.tuwaiqpay.com.sa/api/v1")[0] is False  # not https
    assert validate_gateway_base("https://169.254.169.254/")[0] is False                         # metadata IP
    assert validate_gateway_base("https://127.0.0.1/")[0] is False                               # loopback IP
    assert validate_gateway_base("file:///etc/passwd")[0] is False
    assert validate_gateway_base("https:///nohost")[0] is False
