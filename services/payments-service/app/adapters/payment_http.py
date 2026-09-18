"""Tiny stdlib HTTP for payment-gateway adapters — JSON, secret-free errors.

Gateway hosts are FIXED config / come from a tenant's sealed MERCHANT_API_BASE (not free caller
input on the request path), so this is not the SSRF surface. Credentials and card data are NEVER
logged — a failed call returns a status code and a parsed body, never the request that carried the
secret. A network failure is reported as status 0 so an adapter can map it to GatewayUnavailable
(retryable) rather than a fake decline.
"""
from __future__ import annotations

import base64
import json as _json
import urllib.error
import urllib.request

_UA = "base-services-payments/1.0"
_TIMEOUT = 30


def _send(url: str, data: bytes | None, headers: dict[str, str], method: str = "POST",
          timeout: int | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"User-Agent": _UA, **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout or _TIMEOUT) as resp:  # nosec B310 - sealed-config hosts
            return resp.status, _parse(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _parse(exc.read())
    except Exception:   # noqa: BLE001 — any transport failure is "unreachable", never a decline
        return 0, {}


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
