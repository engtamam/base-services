"""Domain models — Job and Schedule."""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

# Lifecycle: queued → leased → (done | queued-retry | dead). 'dead' = dead-letter.
QUEUED, LEASED, DONE, DEAD, CANCELED = "queued", "leased", "done", "dead", "canceled"
ACTIVE = (QUEUED, LEASED)


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex}"


@dataclass
class Job:
    id: str
    queue: str = "default"
    tenant: str = ""
    type: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    priority: int = 0                 # higher runs first
    status: str = QUEUED
    attempt: int = 0
    max_attempts: int = 5
    lease_token: str = ""             # fencing token — an ack/heartbeat must match it
    lease_expires: float = 0.0        # visibility timeout; a crashed worker's job returns
    next_run_at: float = 0.0          # when it becomes claimable (delay / retry backoff)
    unique_key: str = ""              # no second identical pending job
    idempotency_key: str = ""         # a retried enqueue returns the same job
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    schedule_id: str = ""             # set when a scheduler run created it
    created: float = 0.0
    updated: float = 0.0
    version: int = 0                  # optimistic-lock counter for the atomic claim CAS

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def public(self) -> dict[str, Any]:
        d = self.to_dict()
        d.pop("lease_token", None)     # the fencing token is a worker secret
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        return cls(**{k: d.get(k, getattr(cls, k, None)) for k in cls.__dataclass_fields__})


@dataclass
class Schedule:
    id: str
    name: str
    cron: str
    queue: str = "default"
    tenant: str = ""
    type: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    priority: int = 0
    singleton: bool = True            # skip a run if the previous one is still active
    enabled: bool = True
    last_run_at: float = 0.0
    next_run_at: float = 0.0
    last_job_id: str = ""
    created: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Schedule":
        return cls(**{k: d.get(k, getattr(cls, k, None)) for k in cls.__dataclass_fields__})
