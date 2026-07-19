"""Domain model — FileObject (metadata; content is content-addressed by sha256)."""
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
class FileObject:
    id: str
    tenant: str
    owner: str
    filename: str
    content_type: str
    size: int
    sha256: str
    created: str = field(default_factory=now)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def storage_key(self) -> str:
        return self.sha256  # content-addressed

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "tenant": self.tenant, "owner": self.owner,
            "filename": self.filename, "content_type": self.content_type,
            "size": self.size, "sha256": self.sha256, "storage_key": self.storage_key,
            "created": self.created, "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FileObject":
        return cls(
            id=d["id"], tenant=d.get("tenant", ""), owner=d.get("owner", ""),
            filename=d["filename"], content_type=d["content_type"], size=int(d["size"]),
            sha256=d["sha256"], created=d.get("created", now()),
            metadata=dict(d.get("metadata") or {}),
        )
