# =============================================================================
# identity-service — Built-in CweGuard checks
# -----------------------------------------------------------------------------
# Concrete, real defenses (not theater). Allowlists/markers come from config
# (zero-hardcoded). Each check names the exact CWE it enforces.
# =============================================================================
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from app.adapters.object_store.keys import validate_key
from app.config import Settings
from app.ports.errors import PortError
from app.security.cwe_guard import GuardCheck

# Default sensitive markers scanned in outputs (security defaults, overridable).
_DEFAULT_MARKERS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "private_key",
    "authorization",
)


class ParameterizedCheck(GuardCheck):
    """CWE-89: a db step must declare it uses parameterized queries."""

    name = "parameterized"
    cwe = "CWE-89"

    def pre(self, ctx: dict[str, Any]) -> None:
        if ctx.get("parameterized") is not True:
            raise self._violate("db step is not marked parameterized (raw SQL risk)")


class SsrfAllowlistCheck(GuardCheck):
    """CWE-918: an egress target host must be in the configured allowlist."""

    name = "ssrf_allowlist"
    cwe = "CWE-918"

    def __init__(self, allowed_hosts: frozenset[str]) -> None:
        self._allowed = allowed_hosts

    def pre(self, ctx: dict[str, Any]) -> None:
        url = ctx.get("url")
        if not url:
            raise self._violate("egress step declared no url")
        host = (urlsplit(url).hostname or "").lower()
        if not host:
            raise self._violate("egress url has no host")
        if host not in self._allowed:
            raise self._violate(f"egress host not allow-listed: {host}")


class SafeObjectKeyCheck(GuardCheck):
    """CWE-22: a file step's object key must be path-traversal-safe."""

    name = "safe_object_key"
    cwe = "CWE-22"

    def pre(self, ctx: dict[str, Any]) -> None:
        key = ctx.get("key")
        if not isinstance(key, str):
            raise self._violate("file step declared no key")
        try:
            validate_key(key)
        except PortError as exc:
            raise self._violate(str(exc)) from exc


class NoSecretLeakCheck(GuardCheck):
    """CWE-200/532: a response must not carry sensitive markers."""

    name = "no_secret_leak"
    cwe = "CWE-200"

    def __init__(self, markers: tuple[str, ...] = _DEFAULT_MARKERS) -> None:
        self._markers = tuple(m.lower() for m in markers)

    def _scan(self, value: Any) -> str | None:
        if isinstance(value, dict):
            for k, v in value.items():
                if any(m in str(k).lower() for m in self._markers):
                    return str(k)
                found = self._scan(v)
                if found:
                    return found
        elif isinstance(value, (list, tuple)):
            for item in value:
                found = self._scan(item)
                if found:
                    return found
        return None

    def post(self, ctx: dict[str, Any], result: Any) -> None:
        leaked = self._scan(result)
        if leaked is not None:
            raise self._violate(f"response exposes sensitive field: {leaked}")


def build_checks(settings: Settings) -> dict[str, GuardCheck]:
    """Construct the built-in checks with config-sourced allowlists."""
    checks: list[GuardCheck] = [
        ParameterizedCheck(),
        SsrfAllowlistCheck(settings.egress_allowed_hosts),
        SafeObjectKeyCheck(),
        NoSecretLeakCheck(),
    ]
    return {c.name: c for c in checks}
