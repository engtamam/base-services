"""Domain model — Document. A document is `fields` (arbitrary) plus indexing metadata.

`field_boosts` on an index weight where a term appears (title > body). `tenant` is the hard
isolation key — a query is always constrained to the caller's tenant, server-side.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# Default per-field boost when an index doesn't specify one.
DEFAULT_BOOST = 1.0


@dataclass
class Document:
    id: str
    index: str
    tenant: str
    fields: dict[str, Any] = field(default_factory=dict)
    owner: str = ""
    created: float = 0.0

    def to_record(self, *, doc_len: float) -> dict[str, Any]:
        return {"id": self.id, "index": self.index, "tenant": self.tenant, "fields": self.fields,
                "owner": self.owner, "created": self.created, "doc_len": doc_len}

    @classmethod
    def from_dict(cls, d: dict) -> "Document":
        return cls(id=d["id"], index=d.get("index", ""), tenant=d.get("tenant", ""),
                   fields=dict(d.get("fields") or {}), owner=d.get("owner", ""),
                   created=float(d.get("created", 0) or 0))
