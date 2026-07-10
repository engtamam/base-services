"""Tiny stdlib HTTP helper for provider adapters (no third-party dependency).

Providers target FIXED, trusted host constants (not caller input), so this is not
the SSRF surface; the CWE egress-guard covers user-supplied URLs. Always sends a
User-Agent (some APIs, e.g. Resend, reject requests without one).
"""
from __future__ import annotations

import base64
import json as _json
import urllib.error
import urllib.parse
import urllib.request

_UA = "base-services-notification/1.0"
# Outbound timeout (seconds). Default; overridden once at composition via configure().
_timeout = 15


def configure(timeout_seconds: int) -> None:
    """Set the outbound HTTP timeout from config (called by the container)."""
    global _timeout
    _timeout = max(1, timeout_seconds)


def _request(url: str, data: bytes, headers: dict[str, str]) -> tuple[int, str]:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": _UA, **headers}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_timeout) as resp:  # nosec B310 - fixed https provider hosts
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception:
        return 0, "request failed"


def post_json(url: str, payload: dict, headers: dict[str, str] | None = None) -> tuple[int, str]:
    body = _json.dumps(payload).encode()
    return _request(url, body, {"Content-Type": "application/json", **(headers or {})})


def post_form(url: str, fields: dict[str, str], headers: dict[str, str] | None = None) -> tuple[int, str]:
    body = urllib.parse.urlencode(fields).encode()
    return _request(url, body, {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})})


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def basic(user: str, password: str) -> dict[str, str]:
    raw = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


def ok(status: int) -> bool:
    return 200 <= status < 300
