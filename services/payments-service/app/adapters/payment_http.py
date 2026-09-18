"""Tiny stdlib HTTP for payment-gateway adapters — JSON, secret-free errors.

Gateway hosts are FIXED config / come from a tenant's sealed MERCHANT_API_BASE (not free caller
input on the request path), so this is not the SSRF surface. Credentials and card data are NEVER
logged — a failed call returns a status code and a parsed body, never the request that carried the
secret. A network failure is reported as status 0 so an adapter can map it to GatewayUnavailable
(retryable) rather than a fake decline.
"""
from __future__ import annotations

import base64
import ipaddress
import json as _json
import logging
import socket
import urllib.error
import urllib.request
from urllib.parse import urlsplit

_log = logging.getLogger("app.payment_http")

_UA = "base-services-payments/1.0"
_TIMEOUT = 30

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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects — a 3xx to an internal host would bypass the pre-flight IP guard."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise urllib.error.HTTPError(newurl, code, "redirects are not followed", headers, fp)


_OPENER = urllib.request.build_opener(_NoRedirect())


def guard_fetch_url(url: str) -> str | None:
    """Return a reason string if `url` is unsafe to fetch (SSRF), else None. Resolves the host and
    checks every resolved IP. (A resolve→connect DNS-rebind window remains; acceptable for a
    server-to-server gateway call where the host is a stored credential, not live request input.)"""
    parts = urlsplit(url)
    if parts.scheme not in _ALLOWED_SCHEMES:
        return f"scheme {parts.scheme!r} not allowed"
    host = parts.hostname
    if not host:
        return "missing host"
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except OSError:
        return "host does not resolve"
    for info in infos:
        if _ip_is_blocked(info[4][0]):
            return f"host resolves to a blocked address ({info[4][0]})"
    return None


def _send(url: str, data: bytes | None, headers: dict[str, str], method: str = "POST",
          timeout: int | None = None) -> tuple[int, dict]:
    reason = guard_fetch_url(url)
    if reason is not None:
        # Refuse the SSRF attempt. Report as unreachable (status 0) so the adapter maps it to
        # GatewayUnavailable — never a fabricated decline — and log it (visible, not silent).
        _log.warning("payment_http refused an unsafe gateway URL: %s (host=%s)",
                     reason, urlsplit(url).hostname)
        return 0, {}
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"User-Agent": _UA, **headers})
    try:
        with _OPENER.open(req, timeout=timeout or _TIMEOUT) as resp:  # nosec B310 - guarded above
            return resp.status, _parse(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _parse(exc.read())
    except Exception:   # noqa: BLE001 — any transport failure is "unreachable", never a decline
        return 0, {}


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
