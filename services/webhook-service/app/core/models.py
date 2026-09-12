"""Domain models — Endpoint, Event, Delivery, InboundEvent."""
from __future__ import annotations

import datetime as _dt
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

# Delivery lifecycle. `dead` = exhausted retries → dead-letter. Never "dropped".
QUEUED, DELIVERING, DELIVERED, FAILED, DEAD = "queued", "delivering", "delivered", "failed", "dead"


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex}"


def now_ts() -> float:
    return _dt.datetime.now(_dt.timezone.utc).timestamp()


def iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


@dataclass
class Endpoint:
    id: str
    tenant: str
    url: str
    event_types: list[str] = field(default_factory=list)   # ["*"] = all
    secrets: list[str] = field(default_factory=list)       # [primary, previous] during rotation
    # When the PREVIOUS secret (secrets[1]) stops being accepted, as a unix timestamp. Deliveries
    # dual-sign with the previous secret only while now < this; then it lapses on its own — an
    # overlap that ends by timestamp, never a manual step. 0.0 = no live previous.
    prev_secret_expires: float = 0.0
    enabled: bool = True
    description: str = ""
    # circuit breaker state (stored, not in-process — replicas share it)
    circuit_state: str = "closed"
    consecutive_failures: int = 0
    opened_at: float = 0.0
    # monitoring
    success_count: int = 0
    failure_count: int = 0
    last_status: int = 0
    last_error: str = ""
    last_delivery_at: float = 0.0
    created: str = ""

    def subscribes_to(self, event_type: str) -> bool:
        return "*" in self.event_types or event_type in self.event_types

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("secrets", None)                              # never expose signing secrets
        d["has_secret"] = bool(self.secrets)
        return d

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Endpoint":
        return cls(**{k: d.get(k, getattr(cls, k, None)) for k in cls.__dataclass_fields__})


@dataclass
class Event:
    id: str                       # STABLE across every retry — consumers dedupe on it
    tenant: str
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    created: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        return cls(**{k: d.get(k, getattr(cls, k, None)) for k in cls.__dataclass_fields__})


@dataclass
class Delivery:
    id: str
    tenant: str
    event_id: str
    endpoint_id: str
    status: str = QUEUED
    attempt: int = 0
    response_code: int = 0
    response_body: str = ""       # truncated
    latency_ms: int = 0
    error: str = ""
    next_retry_at: float | None = None
    created: str = ""
    updated: str = ""
    # Optimistic-lock version for the atomic DELIVERING claim. Starts at 1 (not 0) so the first
    # compare-and-set on an existing row uses the Postgres cas UPDATE branch — cas(expected=0) there is
    # INSERT..ON CONFLICT DO NOTHING and would never match a row that already exists.
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Delivery":
        return cls(**{k: d.get(k, getattr(cls, k, None)) for k in cls.__dataclass_fields__})


@dataclass
class InboundEvent:
    id: str
    source: str                   # "stripe", "github", …
    provider_event_id: str        # dedupe key — NEVER a body hash
    payload: dict[str, Any] = field(default_factory=dict)
    received: str = ""
    received_ts: float = 0.0      # drives the dedupe window (INBOUND_DEDUPE_TTL_SECONDS)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
