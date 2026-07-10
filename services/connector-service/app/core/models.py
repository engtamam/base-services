"""Domain models — Connection, ProviderSpec, SyncRun, SyncRecord (DLQ)."""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

ACTIVE, REVOKED, NEEDS_REAUTH = "active", "revoked", "needs_reauth"


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex}"


@dataclass
class Connection:
    id: str
    tenant: str
    provider: str
    status: str = ACTIVE
    # credentials are SEALED (AES-GCM) — never returned by any API, never logged
    access_sealed: str = ""
    refresh_sealed: str = ""
    expires_at: float = 0.0            # 0 = non-expiring token
    scopes: list[str] = field(default_factory=list)
    instance_url: str = ""             # self-hosted providers (SSRF-validated)
    spec_id: str = ""
    # per-resource incremental watermark — advanced ONLY after a batch commits
    watermarks: dict[str, str] = field(default_factory=dict)
    # circuit breaker + monitoring
    circuit_state: str = "closed"
    consecutive_failures: int = 0
    opened_at: float = 0.0
    sync_count: int = 0
    failure_count: int = 0
    last_error: str = ""
    last_sync_at: float = 0.0
    created: float = 0.0

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        for secret in ("access_sealed", "refresh_sealed"):
            d.pop(secret, None)        # credentials never cross the API boundary
        d["connected"] = bool(self.access_sealed)
        return d

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Connection":
        return cls(**{k: d.get(k, getattr(cls, k, None)) for k in cls.__dataclass_fields__})


@dataclass
class ProviderSpec:
    """Makes a provider CONFIG rather than code (see adapters/provider/oauth2_rest.py)."""
    id: str
    tenant: str
    name: str
    token_url: str = ""
    base_url: str = ""
    revoke_url: str = ""
    auth_style: str = "body"           # body | basic
    client_id: str = ""
    client_secret_sealed: str = ""     # sealed at rest
    endpoints: dict[str, Any] = field(default_factory=dict)
    field_map: dict[str, Any] = field(default_factory=dict)
    created: float = 0.0

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("client_secret_sealed", None)
        d["has_client_secret"] = bool(self.client_secret_sealed)
        return d

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ProviderSpec":
        return cls(**{k: d.get(k, getattr(cls, k, None)) for k in cls.__dataclass_fields__})


@dataclass
class SyncRun:
    id: str
    tenant: str
    connection_id: str
    resource: str
    direction: str = "pull"            # pull | push
    status: str = "running"            # running | succeeded | partial | failed
    records_in: int = 0
    records_out: int = 0
    records_failed: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0
    error: str = ""
    watermark_before: str = ""
    watermark_after: str = ""

    @property
    def error_rate(self) -> float:
        total = self.records_in or self.records_out
        return (self.records_failed / total) if total else 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["error_rate"] = round(self.error_rate, 4)
        d["duration"] = round(max(0.0, self.finished_at - self.started_at), 3)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "SyncRun":
        return cls(**{k: d.get(k, getattr(cls, k, None)) for k in cls.__dataclass_fields__})


@dataclass
class SyncRecord:
    """A single record that failed — dead-lettered, never silently dropped."""
    id: str
    tenant: str
    connection_id: str
    run_id: str
    resource: str
    idempotency_key: str
    record: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    attempt: int = 0
    created: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SyncRecord":
        return cls(**{k: d.get(k, getattr(cls, k, None)) for k in cls.__dataclass_fields__})
