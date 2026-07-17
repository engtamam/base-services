"""SSRF guard for source URLs (CWE-918).

A source URL (rtsp/rtmp/http/onvif) is attacker-influenced input. Left unchecked, "register a camera
at http://169.254.169.254/…" turns the stream engine into a proxy into the cluster / cloud metadata.
So every URL is validated on register AND on every (re)connect:

  * scheme allow-listed (rtsp/rtmps/rtmp/http/https),
  * host resolved to IPs, and every resolved IP required to be public — UNLESS `allow_private` is set
    (on-prem NVRs live on 10.x/192.168.x), in which case private ranges are allowed but loopback,
    link-local, and cloud-metadata IPs are STILL blocked,
  * optional host/CIDR allowlist (if configured, the host must match),
  * the validated IP is returned so the caller can PIN the connection to it (DNS-rebinding safe: the
    name can't resolve to a public IP at check time and a private one at connect time).
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

_ALLOWED_SCHEMES = frozenset({"rtsp", "rtsps", "rtmp", "rtmps", "http", "https"})
# Always blocked, even when allow_private=True — these are never a real camera.
_METADATA = frozenset({"169.254.169.254", "fd00:ec2::254", "100.100.100.200"})


class SsrfBlocked(Exception):
    pass


@dataclass(frozen=True)
class Resolved:
    host: str
    ip: str
    port: int | None
    scheme: str


def validate_source_url(url: str, *, allowed_hosts: frozenset[str] = frozenset(),
                        allow_private: bool = False,
                        resolver=None) -> Resolved:
    """Return the resolved+pinned target, or raise SsrfBlocked. `resolver(host)->[ip,…]` is injectable
    for tests; defaults to DNS."""
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise SsrfBlocked(f"scheme not allowed: {scheme or '(none)'}")
    host = parts.hostname
    if not host:
        raise SsrfBlocked("no host in url")

    if allowed_hosts and not _host_in_allowlist(host, allowed_hosts):
        raise SsrfBlocked("host not in STREAM_ALLOWED_HOSTS")

    ips = (resolver or _default_resolver)(host)
    if not ips:
        raise SsrfBlocked(f"host did not resolve: {host}")
    for ip in ips:
        addr = ipaddress.ip_address(ip)
        if ip in _METADATA or addr.is_multicast or addr.is_unspecified or addr.is_reserved:
            raise SsrfBlocked(f"blocked address: {ip}")
        if (addr.is_loopback or addr.is_link_local):
            raise SsrfBlocked(f"blocked address: {ip}")
        if addr.is_private and not allow_private:
            raise SsrfBlocked(f"private address blocked (set STREAM_ALLOW_PRIVATE for on-prem): {ip}")
    # Pin to the first validated IP.
    return Resolved(host=host, ip=ips[0], port=parts.port, scheme=scheme)


def _host_in_allowlist(host: str, allowed: frozenset[str]) -> bool:
    h = host.lower()
    for entry in allowed:
        if "/" in entry:      # CIDR — match if the host is a literal IP inside it
            try:
                if ipaddress.ip_address(h) in ipaddress.ip_network(entry, strict=False):
                    return True
            except ValueError:
                continue
        elif h == entry:
            return True
    return False


def _default_resolver(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return []
    return list({info[4][0] for info in infos})
