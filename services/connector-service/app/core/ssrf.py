"""SSRF guard for customer-supplied instance URLs (CWE-918).

Self-hosted ERPs (Odoo, on-prem SAP) mean the customer types a URL and *we* call it. That is
the same attack surface as outbound webhooks: `http://169.254.169.254/…` steals our cloud
credentials. We validate the scheme, resolve DNS, require every resolved address to be a
public unicast IP, and **pin** the connection to a validated IP so a rebinding attacker
cannot swap it between check and connect.
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

_METADATA = frozenset({"169.254.169.254", "fd00:ec2::254", "100.100.100.200", "192.0.0.192"})
_ALLOWED_SCHEMES = frozenset({"http", "https"})


class SsrfBlocked(Exception):
    """Not a safe outbound destination. Terminal — never retried."""


@dataclass
class Target:
    url: str
    host: str
    port: int
    ip: str


def _public(ip: ipaddress._BaseAddress) -> bool:
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified)


def validate(url: str, *, require_https: bool = True, allow_private: bool = False,
             allowed_hosts: frozenset[str] = frozenset()) -> Target:
    parts = urlsplit(url)
    scheme, host, port = parts.scheme.lower(), (parts.hostname or "").lower(), parts.port

    if scheme not in _ALLOWED_SCHEMES:
        raise SsrfBlocked(f"scheme not allowed: {scheme or '(none)'}")
    if require_https and scheme != "https":
        raise SsrfBlocked("https is required")
    if not host:
        raise SsrfBlocked("missing host")
    if parts.username or parts.password:
        raise SsrfBlocked("credentials in URL are not allowed")
    if allowed_hosts and host not in allowed_hosts:
        raise SsrfBlocked(f"host is not allow-listed: {host}")
    if host in _METADATA:
        raise SsrfBlocked("cloud metadata endpoint")

    port = port or (443 if scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise SsrfBlocked(f"cannot resolve host: {host}") from exc

    validated: list[str] = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if str(ip) in _METADATA:
            raise SsrfBlocked("cloud metadata endpoint")   # even with allow_private
        if not allow_private and not _public(ip):
            raise SsrfBlocked(f"non-public address for {host}: {ip}")
        validated.append(str(ip))
    if not validated:
        raise SsrfBlocked(f"no addresses for host: {host}")
    return Target(url=url, host=host, port=port, ip=validated[0])
