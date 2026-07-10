"""Stdlib HTTP for connector adapters — secret-free errors, size-capped, Retry-After aware."""
from __future__ import annotations

import base64
import json as _json
import urllib.error
import urllib.parse
import urllib.request

from app.ports.connector_provider import CredentialInvalid, ProviderUnavailable, RateLimited

_UA = "base-services-connector/1.0"
TIMEOUT = 20
MAX_BYTES = 1_048_576


def configure(*, timeout_seconds: int, max_response_bytes: int) -> None:
    """Bind the config knobs (HTTP_TIMEOUT_SECONDS / MAX_RESPONSE_BYTES) at container build."""
    global TIMEOUT, MAX_BYTES
    TIMEOUT = max(1, int(timeout_seconds))
    MAX_BYTES = max(1024, int(max_response_bytes))


def _request(url: str, data: bytes | None, headers: dict[str, str], method: str) -> tuple[int, dict, dict]:
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
