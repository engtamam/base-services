"""Tiny stdlib HTTP for payment adapters — JSON + form, secret-free errors.

Provider hosts are FIXED config (not caller input), so this is not the SSRF surface;
the CWE egress-guard covers user-supplied URLs. Card data/keys are NEVER logged.
"""
from __future__ import annotations

import base64
import json as _json
import urllib.error
import urllib.parse
import urllib.request

_UA = "base-services-billing/1.0"
# The default when a caller does not pass one. Callers that have a configured timeout should pass
# it — a value baked in here cannot be tuned by the platform deploying this.
_TIMEOUT = 30


def _send(url: str, data: bytes, headers: dict[str, str], method: str = "POST",
          timeout: int | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"User-Agent": _UA, **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout or _TIMEOUT) as resp:  # nosec B310 - fixed config hosts
            return resp.status, _parse(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _parse(exc.read())
    except Exception:
        return 0, {}


def post_json(url: str, payload: dict, headers: dict[str, str] | None = None,
              timeout: int | None = None) -> tuple[int, dict]:
    return _send(url, _json.dumps(payload).encode(),
                 {"Content-Type": "application/json", "Accept": "application/json", **(headers or {})},
                 timeout=timeout)


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
