"""ConnectorProvider port — talk to one third-party system.

`exchange` (auth code → tokens), `refresh` (rotate), `fetch` (incremental pull),
`push` (write), `revoke` (disconnect). A provider outage raises ProviderUnavailable
(retryable); a rejected credential raises CredentialInvalid (terminal — stop retrying,
the connection needs re-authorising).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from app.ports.errors import PortError


class ProviderUnavailable(PortError):
    """Network/5xx/timeout — retryable."""


class CredentialInvalid(PortError):
    """The provider rejected the credential (401/invalid_grant). Terminal: re-auth needed."""


class RateLimited(PortError):
    """429/503 — carries the provider's Retry-After (seconds) when it supplied one."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class Tokens:
    access_token: str = ""
    refresh_token: str = ""      # may be ROTATED by the provider — always store what comes back
    expires_in: int = 0          # seconds; 0 = non-expiring (e.g. classic Slack bot tokens)
    scopes: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Page:
    records: list[dict[str, Any]] = field(default_factory=list)
    next_cursor: str = ""        # "" = no more pages


@runtime_checkable
class ConnectorProvider(Protocol):
    driver: str

    def exchange(self, *, code: str, redirect_uri: str, spec: dict[str, Any]) -> Tokens:
        ...

    def refresh(self, *, refresh_token: str, spec: dict[str, Any]) -> Tokens:
        ...

    def fetch(self, *, access_token: str, resource: str, cursor: str, limit: int,
              spec: dict[str, Any]) -> Page:
        ...

    def push(self, *, access_token: str, resource: str, record: dict[str, Any],
             idempotency_key: str, spec: dict[str, Any]) -> dict[str, Any]:
        ...

    def revoke(self, *, access_token: str, spec: dict[str, Any]) -> bool:
        ...
