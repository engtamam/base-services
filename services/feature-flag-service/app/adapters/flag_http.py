"""Tiny stdlib HTTP for flag/entitlement adapters. Fixed config hosts; secret-free errors."""
from __future__ import annotations

import json as _json
import urllib.error
import urllib.parse
import urllib.request

_UA = "base-services-flags/1.0"
TIMEOUT = 10


def get_json(url: str, headers: dict[str, str] | None = None) -> tuple[int, dict]:
    return _send(url, None, headers or {}, "GET")


def post_json(url: str, payload: dict, headers: dict[str, str] | None = None) -> tuple[int, dict]:
    return _send(url, _json.dumps(payload).encode(),
                 {"Content-Type": "application/json", **(headers or {})}, "POST")


def _send(url: str, data: bytes | None, headers: dict[str, str], method: str) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"User-Agent": _UA, "Accept": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # nosec B310 - fixed config hosts
            return resp.status, _parse(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _parse(exc.read())
    except Exception:
        return 0, {}


def _parse(raw: bytes) -> dict:
    try:
        val = _json.loads(raw.decode("utf-8", "replace"))
        return val if isinstance(val, dict) else {"_list": val}
    except Exception:
        return {}
