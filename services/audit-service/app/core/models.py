"""Domain model — AuditEvent (append-only)."""
from __future__ import annotations

import datetime as _dt
import uuid
from dataclasses import dataclass, field
from typing import Any


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class AuditEvent:
    id: str
    seq: int
    ts: str
    service: str
    action: str
    actor: str = ""
    resource: str = ""
    tenant: str = ""
    ip: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    prev_hash: str = ""
    hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "seq": self.seq, "ts": self.ts, "service": self.service,
            "action": self.action, "actor": self.actor, "resource": self.resource,
            "tenant": self.tenant, "ip": self.ip, "metadata": self.metadata,
            "prev_hash": self.prev_hash, "hash": self.hash,
        }
