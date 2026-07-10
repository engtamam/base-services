"""Domain models — Event, Rollup, ReportDef."""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

OTHER = "__other__"          # the cardinality spill bucket

# Property keys we refuse to store unless explicitly allow-listed — analytics is where PII leaks.
PII_SUSPECT = frozenset({
    "email", "e_mail", "phone", "mobile", "password", "ssn", "national_id", "iqama",
    "credit_card", "card_number", "iban", "address", "full_name", "dob", "date_of_birth",
})


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex}"


@dataclass
class Event:
    id: str                       # the idempotency key — an at-least-once bus may redeliver
    tenant: str
    name: str                     # e.g. "checkout.completed"
    event_time: float = 0.0       # WHEN IT HAPPENED — bucketing uses this, never ingest_time
    ingest_time: float = 0.0
    subject: str = ""             # user/session id — the unique-count identity
    value: float = 0.0            # numeric payload for sum/percentile aggregations
    props: dict[str, Any] = field(default_factory=dict)   # dimensions (PII-filtered)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        return cls(**{k: d.get(k, getattr(cls, k, None)) for k in cls.__dataclass_fields__})


@dataclass
class Rollup:
    """One pre-aggregated cell: (tenant, metric, granularity, bucket, dimension-values).

    `hll` and `hist` are MERGEABLE sketches, which is what lets a query combine cells across
    buckets and groups without ever touching a raw event.
    """
    key: str
    tenant: str
    metric: str
    granularity: str
    bucket: str
    dims: dict[str, str] = field(default_factory=dict)
    count: int = 0
    sum: float = 0.0
    hll: dict[str, Any] | None = None
    hist: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Rollup":
        return cls(**{k: d.get(k, getattr(cls, k, None)) for k in cls.__dataclass_fields__})


@dataclass
class ReportDef:
    id: str
    tenant: str
    name: str
    query: dict[str, Any] = field(default_factory=dict)
    created: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ReportDef":
        return cls(**{k: d.get(k, getattr(cls, k, None)) for k in cls.__dataclass_fields__})


def rollup_key(tenant: str, metric: str, granularity: str, bucket: str, dims: dict[str, str]) -> str:
    dim_part = ",".join(f"{k}={v}" for k, v in sorted(dims.items())) if dims else "-"
    return f"{tenant}|{metric}|{granularity}|{bucket}|{dim_part}"
