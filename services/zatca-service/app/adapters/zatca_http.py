"""Tiny stdlib HTTP for zatca adapters (no third-party dep). Secret-free errors.

Sidecar/Fatoora hosts are FIXED config (not caller input) so this is not the SSRF
surface; the CWE egress-guard covers user-supplied URLs. Bodies/keys are NEVER logged.
"""
from __future__ import annotations

import base64
import json as _json
import urllib.error
import urllib.request

_UA = "base-services-zatca/1.0"
_TIMEOUT = 30


def post_json(url: str, payload: dict, headers: dict[str, str] | None = None,
              timeout: int = _TIMEOUT) -> tuple[int, dict]:
    body = _json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"User-Agent": _UA, "Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - fixed https config hosts
            return resp.status, _parse(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _parse(exc.read())
    except Exception:
        return 0, {}   # network/transport error (no HTTP answer) — caller treats as unreachable


def _parse(raw: bytes) -> dict:
    try:
        val = _json.loads(raw.decode("utf-8", "replace"))
        return val if isinstance(val, dict) else {}
    except Exception:
        return {}


def basic_auth(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
