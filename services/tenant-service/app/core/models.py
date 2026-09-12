"""Domain model — Tenant. Generic (no product-specific fields)."""
from __future__ import annotations

import datetime as _dt
import uuid
from dataclasses import dataclass, field
from typing import Any

STATUSES = ("pending", "active", "suspended", "deleted")
PENDING, ACTIVE, SUSPENDED, DELETED = STATUSES


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class Tenant:
    id: str
    slug: str
    name: str
    status: str = "pending"
    isolation_mode: str = "shared-row"
    domains: list[str] = field(default_factory=list)
    branding: dict[str, Any] = field(default_factory=dict)      # logo, colors, email_sender
    config: dict[str, Any] = field(default_factory=dict)        # locale, timezone, calendar, feature_flags, limits
    custom_terms: str = ""
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)
    version: int = 0          # optimistic-lock counter — every lifecycle/config write CASes on it
    # Set only on a TOMBSTONE. Its presence is the fact a peer that missed `tenant.deleted` needs
    # to be able to READ — see TenantRepository.tombstone().
    deleted_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "slug": self.slug, "name": self.name, "status": self.status,
            "isolation_mode": self.isolation_mode, "domains": self.domains,
            "branding": self.branding, "config": self.config,
            "custom_terms": self.custom_terms,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "version": self.version,
            "deleted_at": self.deleted_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Tenant":
        # `slug` and `name` are .get(), not d[...], because a TOMBSTONE deliberately drops them:
        # a tenant delete is often an erasure, and the marker proving they are gone must not be a
        # copy of them. Only `id` is truly required — it is the key.
        return cls(
            id=d["id"], slug=d.get("slug", ""), name=d.get("name", ""),
            status=d.get("status", "pending"),
            isolation_mode=d.get("isolation_mode", "shared-row"),
            domains=list(d.get("domains") or []), branding=dict(d.get("branding") or {}),
            config=dict(d.get("config") or {}), custom_terms=d.get("custom_terms", ""),
            created_at=d.get("created_at", now()), updated_at=d.get("updated_at", now()),
            version=int(d.get("version") or 0),
            deleted_at=d.get("deleted_at", ""),
        )

    def public(self) -> dict[str, Any]:
        return self.to_dict()

    def touch(self) -> None:
        self.updated_at = now()
