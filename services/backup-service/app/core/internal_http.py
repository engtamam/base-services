"""Calling a target service's internal endpoints.

Every call carries a target's internal token and reaches a URL that names an internal service, so
the host is checked against the egress allowlist before the request is made — a wrong or
attacker-influenced host would receive that token and the reply. The allowlist is what makes the
destination a decision an operator made rather than whatever the registry happened to hold.
"""
from __future__ import annotations

import json as _json
import urllib.error
import urllib.parse
import urllib.request

_UA = "base-services-backup/1.0"


class TargetUnreachable(RuntimeError):
    """A target that could not be reached or answered with a retryable failure.

    Distinct from a refusal: a target that is down is MISSED for this run and tried again, never
    silently skipped, because 'we backed up' while one service was absent is the exact lie the
    whole phase exists to kill.
    """


def _check_host(url: str, allowed_hosts: frozenset[str]) -> str:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if allowed_hosts and host not in allowed_hosts:
        raise TargetUnreachable(
            f"target host {host!r} is not in EGRESS_ALLOWED_HOSTS — this call carries an internal "
            f"token and must not reach an unlisted host")
    return host


def post_json(url: str, payload: dict, token: str, *, allowed_hosts: frozenset[str],
              timeout_seconds: int) -> tuple[int, dict]:
    """POST a JSON body with the internal token. Returns (status, parsed body)."""
    _check_host(url, allowed_hosts)
    data = _json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"User-Agent": _UA, "Content-Type": "application/json",
                 "Accept": "application/json", "X-Internal-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:  # nosec B310 - allowlisted host
            return resp.status, _parse(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _parse(exc.read())
    except Exception as exc:  # noqa: BLE001 — DNS, connect, timeout: all "unreachable this run"
        raise TargetUnreachable(f"{url}: {type(exc).__name__}") from exc




def _parse(raw: bytes) -> dict:
    try:
        val = _json.loads(raw.decode("utf-8", "replace"))
        return val if isinstance(val, dict) else {"_list": val}
    except Exception:  # noqa: BLE001
        return {}
