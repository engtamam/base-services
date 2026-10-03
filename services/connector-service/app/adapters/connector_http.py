"""Stdlib HTTP for connector adapters — secret-free errors, size-capped, Retry-After aware."""
from __future__ import annotations

import base64
import http.client
import json as _json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request

from app.core import ssrf as _ssrf
from app.ports.connector_provider import CredentialInvalid, ProviderUnavailable, RateLimited

_UA = "base-services-connector/1.0"
TIMEOUT = 20
MAX_BYTES = 1_048_576
# SSRF policy, bound at container build. Every outbound connector fetch (token/revoke/api) is
# validated against it AT CALL TIME — not only when a spec is registered — so a spec that passed
# registration cannot later resolve to an internal/metadata address (the DNS-rebind window) and be
# fetched. Defaults are the safe ones (https required, no private IPs) until configure() runs.
_REQUIRE_HTTPS = True
_ALLOW_PRIVATE = False
_ALLOWED_HOSTS: tuple[str, ...] = ()


def configure(*, timeout_seconds: int, max_response_bytes: int, require_https: bool = True,
              allow_private: bool = False, allowed_hosts: tuple[str, ...] = ()) -> None:
    """Bind the config knobs (HTTP_TIMEOUT_SECONDS / MAX_RESPONSE_BYTES) and the SSRF policy."""
    global TIMEOUT, MAX_BYTES, _REQUIRE_HTTPS, _ALLOW_PRIVATE, _ALLOWED_HOSTS
    TIMEOUT = max(1, int(timeout_seconds))
    MAX_BYTES = max(1024, int(max_response_bytes))
    _REQUIRE_HTTPS = bool(require_https)
    _ALLOW_PRIVATE = bool(allow_private)
    _ALLOWED_HOSTS = tuple(allowed_hosts or ())


def _pinned_opener(target: _ssrf.Target) -> urllib.request.OpenerDirector:
    """An opener that connects to the VALIDATED ip and never re-resolves — closing the DNS-rebind
    window between validate() and connect(). The Host header + TLS SNI keep the original hostname, so
    vhost routing and certificate verification are unchanged; only the socket target is pinned."""
    ip, host, port = target.ip, target.host, target.port

    class _PinnedHTTPS(http.client.HTTPSConnection):
        def connect(self):
            sock = socket.create_connection((ip, port), self.timeout)
            self.sock = self._context.wrap_socket(sock, server_hostname=host)   # SNI + cert = host

    class _PinnedHTTP(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.create_connection((ip, port), self.timeout)

    class _HTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(_PinnedHTTPS, req, context=ssl.create_default_context())

    class _HTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(_PinnedHTTP, req)

    # connector-006: never follow a redirect. The opener is PINNED to the one validated ip, so a
    # 3xx Location would either be fetched at the wrong (pinned) address or, worse, re-resolved to an
    # un-validated host — the DNS-rebind/SSRF hole this adapter exists to close. Refuse it outright;
    # a provider that needs a different URL must be configured with that URL.
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise _RedirectRefused(f"redirect refused ({code})")

    return urllib.request.build_opener(_HTTPSHandler, _HTTPHandler, _NoRedirect)


class _RedirectRefused(Exception):
    """A provider response tried to redirect; the pinned opener refuses (connector-006)."""


class ResponseTooLarge(Exception):
    """The provider response exceeded MAX_BYTES (connector-007)."""


def _request(url: str, data: bytes | None, headers: dict[str, str], method: str) -> tuple[int, dict, dict]:
    # SSRF gate on EVERY outbound fetch — blocks internal/metadata targets and private ranges even for
    # a URL that was valid at registration. A blocked URL is a permanent provider error, never retried.
    try:
        target = _ssrf.validate(url, require_https=_REQUIRE_HTTPS, allow_private=_ALLOW_PRIVATE,
                                allowed_hosts=_ALLOWED_HOSTS)
    except _ssrf.SsrfBlocked as exc:
        raise ProviderUnavailable(f"URL_REJECTED: {exc}") from None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"User-Agent": _UA, "Accept": "application/json", **headers})
    try:
        # PIN to the validated ip — urllib's default opener would re-resolve the host (the rebind hole).
        with _pinned_opener(target).open(req, timeout=TIMEOUT) as resp:  # nosec B310 - pinned to a validated public ip
            return resp.status, _parse(_read_capped(resp), strict=True), dict(resp.getheaders())
    except urllib.error.HTTPError as exc:
        body = _parse(_read_capped(exc))
        headers_out = dict(exc.headers or {})
        if exc.code in (401, 403):
            raise CredentialInvalid(f"provider rejected credential ({exc.code})") from None
        if exc.code in (429, 503):
            ra = headers_out.get("Retry-After") or headers_out.get("retry-after")
            raise RateLimited(f"rate limited ({exc.code})", _to_float(ra)) from None
        if exc.code >= 500:
            raise ProviderUnavailable(f"provider error {exc.code}") from None
        return exc.code, body, headers_out
    except ResponseTooLarge:
        raise ProviderUnavailable("response too large") from None
    except _RedirectRefused as exc:
        raise ProviderUnavailable(str(exc)) from None
    except (CredentialInvalid, RateLimited, ProviderUnavailable):
        raise
    except Exception as exc:
        raise ProviderUnavailable(type(exc).__name__) from None   # secret-free


def _read_capped(resp) -> bytes:
    # connector-007: read one byte PAST the cap. If it is there, the body was truncated — raise rather
    # than silently returning a half body that _parse would turn into {} (an oversized response then
    # looked like an empty/clean one). A real HTTPRedirectHandler-wrapped error reader works the same.
    raw = resp.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ResponseTooLarge()
    return raw


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def post_form(url: str, fields: dict, headers: dict[str, str] | None = None) -> tuple[int, dict, dict]:
    return _request(url, urllib.parse.urlencode(fields).encode(),
                    {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})}, "POST")


def post_json(url: str, payload: dict, headers: dict[str, str] | None = None) -> tuple[int, dict, dict]:
    return _request(url, _json.dumps(payload).encode(),
                    {"Content-Type": "application/json", **(headers or {})}, "POST")


def get_json(url: str, headers: dict[str, str] | None = None) -> tuple[int, dict, dict]:
    return _request(url, None, headers or {}, "GET")


def _parse(raw: bytes, *, strict: bool = False) -> dict:
    # connector-007: an EMPTY body is a legitimate "no content" → {}. A non-empty body that does not
    # parse is a different fact: on the success path (strict) that is a malformed provider response,
    # not an empty one, so raise rather than masquerade as {}. Error-path bodies stay lenient (a 4xx
    # may legitimately carry HTML), best-effort for diagnostics.
    if not raw:
        return {}
    try:
        val = _json.loads(raw.decode("utf-8", "replace"))
        return val if isinstance(val, dict) else {"_list": val}
    except Exception:
        if strict:
            raise ProviderUnavailable("invalid provider response (not JSON)") from None
        return {}


def basic_auth(user: str, password: str = "") -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def bearer(token: str) -> str:
    return f"Bearer {token}"
