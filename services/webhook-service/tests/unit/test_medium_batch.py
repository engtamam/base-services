"""Non-security MEDIUM remediation: DNS-rebinding / IP-pinning defense is asserted by a test, not just a
comment (webhook-010)."""
from __future__ import annotations

import socket

import pytest

from app.core import ssrf


def _infos(ips):
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0)) for ip in ips]


def test_public_host_resolving_to_private_is_blocked(monkeypatch):
    # A public-looking host that resolves to a private address (DNS rebinding) must be refused.
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _infos(["10.0.0.5"]))
    with pytest.raises(ssrf.SsrfBlocked):
        ssrf.validate("https://totally-public.example/hook")


def test_mixed_public_and_private_resolution_is_blocked(monkeypatch):
    # ANY non-public address disqualifies the host — an attacker needs only one poisoned answer.
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _infos(["93.184.216.34", "127.0.0.1"]))
    with pytest.raises(ssrf.SsrfBlocked):
        ssrf.validate("https://totally-public.example/hook")


def test_all_public_resolution_pins_the_ip(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _infos(["93.184.216.34"]))
    target = ssrf.validate("https://totally-public.example/hook")
    assert target.ip == "93.184.216.34"          # connection pinned to the validated address
