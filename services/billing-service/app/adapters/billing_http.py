"""Tiny stdlib HTTP for payment adapters — JSON + form, secret-free errors.

Payment-GATEWAY hosts come from operator/tenant-set credentials (PAYMENT_API_BASE), so this IS an
SSRF surface: a base pointed at cloud metadata / loopback / a private host must not be fetched. Every
outbound call is egress-guarded (scheme + resolved-IP range) before the socket opens. A payment
gateway is always a public https host, so this never blocks a legitimate call. Card data/keys are
NEVER logged.
"""
from __future__ import annotations

import base64
import ipaddress
import json as _json
import logging
import socket
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urlsplit

_log = logging.getLogger("app.billing_http")
_UA = "base-services-billing/1.0"
# The default when a caller does not pass one. Callers that have a configured timeout should pass
# it — a value baked in here cannot be tuned by the platform deploying this.
_TIMEOUT = 30
_ALLOWED_SCHEMES = ("https", "http")


def _ip_is_blocked(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return (a.is_private or a.is_loopback or a.is_link_local or a.is_reserved
            or a.is_multicast or a.is_unspecified)


def guard_fetch_url(url: str) -> str | None:
    """Return a reason if `url` is unsafe to fetch (SSRF), else None. A payment gateway base must be
    a public host; a URL that resolves to a private/loopback/link-local/reserved/metadata address, or
    a non-web scheme, is refused before the socket opens."""
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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects — a 3xx to an internal host would bypass the pre-flight IP guard."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise urllib.error.HTTPError(newurl, code, "redirects are not followed", headers, fp)


_OPENER = urllib.request.build_opener(_NoRedirect())


def _send(url: str, data: bytes | None, headers: dict[str, str], method: str = "POST",
          timeout: int | None = None) -> tuple[int, dict]:
    reason = guard_fetch_url(url)
    if reason is not None:
        _log.warning("billing_http refused an unsafe gateway URL: %s (host=%s)",
                     reason, urlsplit(url).hostname)
        return 0, {}
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"User-Agent": _UA, **headers})
    try:
        with _OPENER.open(req, timeout=timeout or _TIMEOUT) as resp:  # nosec B310 - egress-guarded above
            return resp.status, _parse(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _parse(exc.read())
    except Exception:   # noqa: BLE001 — any transport failure is "unreachable", never a fake decline
        return 0, {}


def post_json(url: str, payload: dict, headers: dict[str, str] | None = None,
              timeout: int | None = None) -> tuple[int, dict]:
    return _send(url, _json.dumps(payload).encode(),
                 {"Content-Type": "application/json", "Accept": "application/json", **(headers or {})},
                 timeout=timeout)


def get_json(url: str, headers: dict[str, str] | None = None,
             timeout: int | None = None) -> tuple[int, dict]:
    """GET a JSON resource — used to pull a payment's current status for reconciliation."""
    return _send(url, None, {"Accept": "application/json", **(headers or {})},
                 method="GET", timeout=timeout)


def post_form(url: str, fields: dict, headers: dict[str, str] | None = None) -> tuple[int, dict]:
    return _send(url, urllib.parse.urlencode(fields, doseq=True).encode(),
                 {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})})


def _parse(raw: bytes) -> dict:
    try:
        val = _json.loads(raw.decode("utf-8", "replace"))
        return val if isinstance(val, dict) else {"_list": val}
    except Exception:
        return {}


def basic_auth(user: str, password: str = "") -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def bearer(token: str) -> str:
    return f"Bearer {token}"
