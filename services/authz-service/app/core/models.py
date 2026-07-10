"""Domain models — Role, Policy, Assignment."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


def new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class Role:
    id: str
    tenant: str
    name: str
    capabilities: list[str] = field(default_factory=list)
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "tenant": self.tenant, "name": self.name,
                "capabilities": self.capabilities, "description": self.description}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Role":
        return cls(id=d["id"], tenant=d.get("tenant", ""), name=d["name"],
                   capabilities=list(d.get("capabilities") or []), description=d.get("description", ""))


@dataclass
class Policy:
    id: str
    tenant: str
    effect: str                         # allow | deny
    action: str = "*"                   # glob
    resource: str = "*"                 # glob
    condition: dict[str, Any] = field(default_factory=dict)
    priority: int = 0
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "tenant": self.tenant, "effect": self.effect,
                "action": self.action, "resource": self.resource,
                "condition": self.condition, "priority": self.priority,
                "description": self.description}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Policy":
        return cls(id=d["id"], tenant=d.get("tenant", ""), effect=d["effect"],
                   action=d.get("action", "*"), resource=d.get("resource", "*"),
                   condition=dict(d.get("condition") or {}), priority=int(d.get("priority", 0)),
                   description=d.get("description", ""))


@dataclass
class Assignment:
    subject: str
    tenant: str
    roles: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)   # direct grants

    def to_dict(self) -> dict[str, Any]:
        return {"subject": self.subject, "tenant": self.tenant,
                "roles": self.roles, "capabilities": self.capabilities}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Assignment":
        return cls(subject=d["subject"], tenant=d.get("tenant", ""),
                   roles=list(d.get("roles") or []), capabilities=list(d.get("capabilities") or []))
