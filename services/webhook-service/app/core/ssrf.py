"""SSRF guard for outbound webhooks (CWE-918) — the #1 vulnerability of any webhook system.

Customers register arbitrary URLs and *we* make the server-side request. Without this,
`http://169.254.169.254/latest/meta-data/iam/security-credentials/` exfiltrates our cloud
credentials, and `http://127.0.0.1:6379` talks to our Redis.

Defence:
  1. scheme allowlist (https required by default) — no file://, gopher://, redis://
  2. no credentials/fragment in the URL, no non-standard ports unless explicitly allowed
  3. resolve the host → **every** resolved address must be a public unicast IP
  4. **pin** the connection to one validated IP (the transport never re-resolves), which
     closes the TOCTOU window a DNS-rebinding attacker needs
  5. optional host allow/deny lists
Re-run `validate()` on **every delivery attempt** — DNS changes between retries.
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

# Cloud instance-metadata endpoints — the classic SSRF prize.
_METADATA = frozenset({"169.254.169.254", "fd00:ec2::254", "100.100.100.200", "192.0.0.192"})
_ALLOWED_SCHEMES = frozenset({"http", "https"})


class SsrfBlocked(Exception):
    """The URL is not a safe outbound destination. Never retried — it is terminal."""


@dataclass
class Target:
    url: str
    host: str
    port: int
    ip: str            # the ONE validated address the transport must connect to


def _ip_is_public(ip: ipaddress._BaseAddress) -> bool:
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
        or ip.is_reserved or ip.is_unspecified
        or getattr(ip, "is_site_local", False)
    )


def _resolve(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise SsrfBlocked(f"cannot resolve host: {host}") from exc
    return [i[4][0] for i in infos]


def validate(url: str, *, require_https: bool = True, allow_private: bool = False,
             allowed_hosts: frozenset[str] = frozenset(),
             denied_hosts: frozenset[str] = frozenset()) -> Target:
    """Validate + resolve + pin. Raises SsrfBlocked; returns the IP to connect to."""
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
    port = port or (443 if scheme == "https" else 80)

    if denied_hosts and host in denied_hosts:
        raise SsrfBlocked(f"host is denied: {host}")
    if allowed_hosts and host not in allowed_hosts:
        raise SsrfBlocked(f"host is not allow-listed: {host}")

    if host in _METADATA:
        raise SsrfBlocked("cloud metadata endpoint")

    addresses = _resolve(host, port)
    if not addresses:
        raise SsrfBlocked(f"no addresses for host: {host}")

    validated: list[str] = []
    for addr in addresses:
        ip = ipaddress.ip_address(addr)
        if str(ip) in _METADATA:
            raise SsrfBlocked("cloud metadata endpoint")
        # Evaluate the deny-list against the RESOLVED IP too (webhook-004): a hostname check alone is
        # bypassed by giving a denied host's literal IP (or a hostname that resolves to a denied IP).
        if denied_hosts and str(ip) in denied_hosts:
            raise SsrfBlocked(f"resolved address is denied: {ip}")
        if not allow_private and not _ip_is_public(ip):
            # ANY non-public address disqualifies the host — a rebinding attacker only
            # needs one poisoned answer, so we refuse to pick "the good one".
            raise SsrfBlocked(f"non-public address for {host}: {ip}")
        validated.append(str(ip))

    return Target(url=url, host=host, port=port, ip=validated[0])
