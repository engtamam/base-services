"""Stdlib HTTP for connector adapters — secret-free errors, size-capped, Retry-After aware."""
from __future__ import annotations

import base64
import json as _json
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


def _request(url: str, data: bytes | None, headers: dict[str, str], method: str) -> tuple[int, dict, dict]:
    # SSRF gate on EVERY outbound fetch — blocks internal/metadata targets and private ranges even for
    # a URL that was valid at registration. A blocked URL is a permanent provider error, never retried.
    try:
        _ssrf.validate(url, require_https=_REQUIRE_HTTPS, allow_private=_ALLOW_PRIVATE,
                       allowed_hosts=_ALLOWED_HOSTS)
    except _ssrf.SsrfBlocked as exc:
        raise ProviderUnavailable(f"URL_REJECTED: {exc}") from None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"User-Agent": _UA, "Accept": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # nosec B310 - validated/pinned hosts
            return resp.status, _parse(resp.read(MAX_BYTES)), dict(resp.getheaders())
    except urllib.error.HTTPError as exc:
        body = _parse(exc.read(MAX_BYTES))
        headers_out = dict(exc.headers or {})
        if exc.code in (401, 403):
            raise CredentialInvalid(f"provider rejected credential ({exc.code})") from None
        if exc.code in (429, 503):
            ra = headers_out.get("Retry-After") or headers_out.get("retry-after")
            raise RateLimited(f"rate limited ({exc.code})", _to_float(ra)) from None
        if exc.code >= 500:
            raise ProviderUnavailable(f"provider error {exc.code}") from None
        return exc.code, body, headers_out
    except Exception as exc:
        raise ProviderUnavailable(type(exc).__name__) from None   # secret-free


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
