"""Tiny stdlib HTTP for payment-gateway adapters — JSON, secret-free errors.

Gateway hosts are FIXED config / come from a tenant's sealed MERCHANT_API_BASE (not free caller
input on the request path), so this is not the SSRF surface. Credentials and card data are NEVER
logged — a failed call returns a status code and a parsed body, never the request that carried the
secret. A network failure is reported as status 0 so an adapter can map it to GatewayUnavailable
(retryable) rather than a fake decline.
"""
from __future__ import annotations

import base64
import http.client
import ipaddress
import json as _json
import logging
import socket
import ssl
from urllib.parse import urlsplit

_log = logging.getLogger("app.payment_http")

_UA = "base-services-payments/1.0"
_TIMEOUT = 30
_TLS = ssl.create_default_context()   # verifies the cert against the ORIGINAL hostname (SNI below)

# The gateway base URL can come from a tenant-supplied MERCHANT_API_BASE, so every outbound fetch is
# a potential SSRF vector. Only https/http to a PUBLIC host is allowed; a URL that resolves to a
# private, loopback, link-local, reserved, multicast or unspecified address (e.g. 169.254.169.254
# cloud metadata, 127.0.0.1, 10.x, ::1) is refused BEFORE the socket is opened. Non-web schemes
# (file://, ftp://, gopher://…) are refused by the scheme check.
_ALLOWED_SCHEMES = ("https", "http")


def _ip_is_blocked(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return (a.is_private or a.is_loopback or a.is_link_local or a.is_reserved
            or a.is_multicast or a.is_unspecified)


def _resolve_pinned(url: str) -> tuple[str | None, str | None]:
    """Resolve the host, validate EVERY resolved IP, and return (None, safe_ip) or (reason, None).

    Returning the exact IP that passed the check lets `_send` CONNECT to that pinned IP instead of
    re-resolving at connect time — closing the resolve→connect DNS-rebind window (audit FIN-012): a
    host that answered a public IP to this lookup then a private/metadata IP to urllib's own connect
    lookup can no longer steer the socket at 169.254.169.254 / an internal address."""
    parts = urlsplit(url)
    if parts.scheme not in _ALLOWED_SCHEMES:
        return f"scheme {parts.scheme!r} not allowed", None
    host = parts.hostname
    if not host:
        return "missing host", None
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except OSError:
        return "host does not resolve", None
    if not infos:
        return "host does not resolve", None
    for info in infos:
        if _ip_is_blocked(info[4][0]):
            return f"host resolves to a blocked address ({info[4][0]})", None
    return None, infos[0][4][0]     # every IP passed; pin the first for the connect


def guard_fetch_url(url: str) -> str | None:
    """Return a reason string if `url` is unsafe to fetch (SSRF), else None."""
    return _resolve_pinned(url)[0]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to a PINNED IP, but SNI + certificate verification + the Host header all use the ORIGINAL
    hostname (`self.host`). So the socket goes only to the IP we already validated, and TLS still proves
    we reached the right host — no DNS-rebind at connect (FIN-012)."""

    def __init__(self, hostname: str, ip: str, port: int, **kw) -> None:
        super().__init__(hostname, port, **kw)      # hostname → Host header + SNI + cert name
        self._ip = ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._ip, self.port), self.timeout, self.source_address)
        if self._tunnel_host:
            self._tunnel()
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Plain HTTP to a PINNED IP, with the Host header set to the original hostname."""

    def __init__(self, hostname: str, ip: str, port: int, **kw) -> None:
        super().__init__(hostname, port, **kw)
        self._ip = ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._ip, self.port), self.timeout, self.source_address)


def _send(url: str, data: bytes | None, headers: dict[str, str], method: str = "POST",
          timeout: int | None = None) -> tuple[int, dict]:
    reason, pinned_ip = _resolve_pinned(url)
    if reason is not None or not pinned_ip:
        # Refuse the SSRF attempt. Report as unreachable (status 0) so the adapter maps it to
        # GatewayUnavailable — never a fabricated decline — and log it (visible, not silent).
        _log.warning("payment_http refused an unsafe gateway URL: %s (host=%s)",
                     reason or "unresolved", urlsplit(url).hostname)
        return 0, {}
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    # Connect to the PINNED, already-validated IP; keep the ORIGINAL hostname for the Host header and
    # (for TLS) SNI + certificate verification. http.client does NOT follow redirects, so a 3xx to an
    # internal host is simply returned, never chased past the guard.
    conn: http.client.HTTPConnection
    if parts.scheme == "https":
        conn = _PinnedHTTPSConnection(host, pinned_ip, port, timeout=timeout or _TIMEOUT, context=_TLS)
    else:
        conn = _PinnedHTTPConnection(host, pinned_ip, port, timeout=timeout or _TIMEOUT)
    try:
        # http.client derives the Host header from `host` (the hostname); do not override it.
        conn.request(method, path, body=data, headers={"User-Agent": _UA, **headers})
        resp = conn.getresponse()
        return resp.status, _parse(resp.read())
    except Exception:   # noqa: BLE001 — any transport failure is "unreachable", never a decline
        return 0, {}
    finally:
        conn.close()


def validate_gateway_base(url: str) -> tuple[bool, str]:
    """Write-time defence-in-depth for MERCHANT_API_BASE: require https and a non-private host. Does
    NOT do DNS (a transient DNS failure must not reject a valid host at write); the fetch-time
    guard resolves. Returns (ok, reason)."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        return False, "gateway base URL must be https"
    if not parts.hostname:
        return False, "gateway base URL has no host"
    # If it is an IP literal, reject private/loopback/etc immediately.
    try:
        ipaddress.ip_address(parts.hostname)
        if _ip_is_blocked(parts.hostname):
            return False, "gateway base URL points at a non-public address"
    except ValueError:
        pass  # a hostname, not an IP — fetch-time guard resolves it
    return True, ""


def post_json(url: str, payload: dict, headers: dict[str, str] | None = None,
              timeout: int | None = None) -> tuple[int, dict]:
    return _send(url, _json.dumps(payload).encode(),
                 {"Content-Type": "application/json", "Accept": "application/json", **(headers or {})},
                 timeout=timeout)


def get_json(url: str, headers: dict[str, str] | None = None,
             timeout: int | None = None) -> tuple[int, dict]:
    return _send(url, None, {"Accept": "application/json", **(headers or {})},
                 method="GET", timeout=timeout)


def _parse(raw: bytes) -> dict:
    try:
        val = _json.loads(raw.decode("utf-8", "replace"))
        return val if isinstance(val, dict) else {"_list": val}
    except Exception:   # noqa: BLE001
        return {}


def bearer(token: str) -> str:
    return f"Bearer {token}"


def basic_auth(user: str, password: str = "") -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
