"""Calling a target service's internal endpoints.

Every call carries a target's internal token and reaches a URL that names an internal service, so
the host is checked against the egress allowlist before the request is made — a wrong or
attacker-influenced host would receive that token and the reply. The allowlist is what makes the
destination a decision an operator made rather than whatever the registry happened to hold.
"""
from __future__ import annotations

import ipaddress
import json as _json
import socket
from app.core.internal_client import assertion_headers_for_url
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


# Cloud instance-metadata endpoints — the classic exfil prize; blocked unconditionally.
_METADATA_HOSTS = frozenset({"169.254.169.254", "fd00:ec2::254", "100.100.100.200", "192.0.0.192",
                             "metadata.google.internal"})
_METADATA_IPS = frozenset({"169.254.169.254", "fd00:ec2::254", "100.100.100.200", "192.0.0.192"})


def _resolution_is_blocked(host: str) -> str | None:
    """Resolve `host` and return a reason if ANY address is a metadata/link-local/loopback target
    (backup-007). A literal-IP metadata check is bypassed by an allow-listed DNS name — or DNS
    rebinding — that resolves to 169.254.169.254. NOTE: ordinary PRIVATE ranges are NOT blocked here:
    this service's whole job is calling internal services on private ClusterIPs. Only the
    metadata/link-local/loopback classes, which no legitimate target ever uses, are refused."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return None   # a name that won't resolve is a plain TargetUnreachable later, not an SSRF signal
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr.split("%")[0])
        except ValueError:
            continue
        if str(ip) in _METADATA_IPS or ip.is_link_local or ip.is_loopback or ip.is_multicast:
            return f"{host!r} resolves to a blocked address {ip} (metadata/link-local/loopback)"
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow 3xx. Only the INITIAL url is checked against the egress allowlist; the
    default opener would then transparently follow a redirect to a NEW host — sending the internal
    token (and the resolved operator secret in the assertion header) to an unchecked, possibly
    attacker-controlled destination (SSRF / secret exfiltration). Fail closed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401,A002
        raise TargetUnreachable(
            f"refused a {code} redirect to {newurl!r}: a token-bearing egress call must not follow "
            f"redirects — the new host was never allow-listed")


# One opener, redirects disabled, reused for every token-bearing call.
_OPENER = urllib.request.build_opener(_NoRedirect)


def _check_host(url: str, allowed_hosts: frozenset[str]) -> str:
    parts = urllib.parse.urlparse(url)
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    # This call carries the internal token (and can resolve a stored operator secret into an outbound
    # header), so egress is FAIL-CLOSED: without an explicit allowlist nothing may leave. An empty
    # EGRESS_ALLOWED_HOSTS used to skip the check entirely — any host, enabling token/secret exfil to
    # an attacker-registered target.
    if scheme not in ("http", "https"):
        raise TargetUnreachable(f"egress scheme not allowed: {scheme or '(none)'}")
    if host in _METADATA_HOSTS:
        raise TargetUnreachable("egress to a cloud metadata endpoint is refused")
    if not allowed_hosts:
        raise TargetUnreachable(
            "egress is refused: EGRESS_ALLOWED_HOSTS is empty. This call carries an internal token, "
            "so every outbound host must be explicitly allow-listed — an empty list means no egress.")
    if host not in allowed_hosts:
        raise TargetUnreachable(
            f"target host {host!r} is not in EGRESS_ALLOWED_HOSTS — this call carries an internal "
            f"token and must not reach an unlisted host")
    blocked = _resolution_is_blocked(host)
    if blocked is not None:
        raise TargetUnreachable(f"egress refused: {blocked}")
    return host


def post_json(url: str, payload: dict, token: str, *, allowed_hosts: frozenset[str],
              timeout_seconds: int) -> tuple[int, dict]:
    """POST a JSON body with the internal token. Returns (status, parsed body)."""
    _check_host(url, allowed_hosts)
    data = _json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"User-Agent": _UA, "Content-Type": "application/json",
                 "Accept": "application/json", "X-Internal-Token": token,
                 **assertion_headers_for_url(url)})
    try:
        with _OPENER.open(req, timeout=timeout_seconds) as resp:  # nosec B310 - allowlisted host, redirects refused
            return resp.status, _parse(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _parse(exc.read())
    except TargetUnreachable:
        raise                                   # a refused redirect — keep its specific reason
    except Exception as exc:  # noqa: BLE001 — DNS, connect, timeout: all "unreachable this run"
        raise TargetUnreachable(f"{url}: {type(exc).__name__}") from exc




def _parse(raw: bytes) -> dict:
    try:
        val = _json.loads(raw.decode("utf-8", "replace"))
        return val if isinstance(val, dict) else {"_list": val}
    except Exception:  # noqa: BLE001
        return {}
