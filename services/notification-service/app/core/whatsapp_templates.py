"""WhatsApp approved-template registry — generic, content-agnostic.

Meta requires business-initiated WhatsApp messages to use a template it has APPROVED. This service
manages the LIFECYCLE of those templates — register, submit, track status, substitute variables at
send — but holds NO template semantics: `body` is opaque, and the adopter owns the copy. All this
layer knows is the template's identity (`tenant, name, language`), its Meta `category`, its ordered
`variables`, and its approval `status`.

Storage is tenant-scoped, filtered reads only (no scan). A template is added at RUNTIME through the
operator surface — the admin panel registers one, no redeploy.
"""
from __future__ import annotations

import datetime as _dt
import uuid
from dataclasses import dataclass, field
from typing import Any

# Meta's fixed set. A category outside it is refused rather than sent and rejected upstream.
CATEGORIES = ("UTILITY", "MARKETING", "AUTHENTICATION")

PENDING, APPROVED, REJECTED = "pending", "approved", "rejected"


class TemplateError(ValueError):
    """A template definition the registry refuses (bad category/variables/duplicate)."""


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _key(tenant: str, name: str, language: str) -> str:
    # Identity is (tenant, name, language): the same template exists once per language.
    return f"{tenant}|{name}|{language}"


@dataclass
class WhatsAppTemplate:
    id: str
    tenant: str
    name: str
    language: str
    category: str
    body: str = ""                       # opaque to this service; the adopter owns it
    variables: list[str] = field(default_factory=list)   # ordered placeholders the body references
    status: str = PENDING
    provider_template_id: str = ""       # Meta's id, set on submit
    rejection_reason: str = ""
    created: str = field(default_factory=_now)
    updated: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "tenant": self.tenant, "name": self.name, "language": self.language,
                "category": self.category, "body": self.body, "variables": self.variables,
                "status": self.status, "provider_template_id": self.provider_template_id,
                "rejection_reason": self.rejection_reason, "created": self.created,
                "updated": self.updated}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WhatsAppTemplate":
        return cls(id=d["id"], tenant=d.get("tenant", ""), name=d.get("name", ""),
                   language=d.get("language", ""), category=d.get("category", ""),
                   body=d.get("body", ""), variables=list(d.get("variables") or []),
                   status=d.get("status", PENDING),
                   provider_template_id=d.get("provider_template_id", ""),
                   rejection_reason=d.get("rejection_reason", ""),
                   created=d.get("created", _now()), updated=d.get("updated", _now()))

    def public(self) -> dict[str, Any]:
        return self.to_dict()


class WhatsAppTemplateStore:
    collection = "whatsapp_templates"

    def __init__(self, store) -> None:
        self._s = store

    async def save(self, t: WhatsAppTemplate) -> WhatsAppTemplate:
        await self._s.put(self.collection, _key(t.tenant, t.name, t.language), t.to_dict())
        return t

    async def get(self, tenant: str, name: str, language: str) -> WhatsAppTemplate | None:
        row = await self._s.get(self.collection, _key(tenant, name, language))
        return WhatsAppTemplate.from_dict(row) if row else None

    async def find(self, *, tenant: str, status: str = "", limit: int = 100,
                   after: str | None = None) -> tuple[list[dict], str | None]:
        if not tenant:
            raise TemplateError("find requires a tenant — an empty scope is every customer")
        where: dict[str, Any] = {"tenant": tenant}
        if status:
            where["status"] = status
        return await self._s.find(self.collection, where, limit=limit, after=after,
                                  sort=("created", "desc"))


class WhatsAppTemplateRegistry:
    """Register / list / get. Submission + status live in the template manager (54.3)."""

    def __init__(self, store: WhatsAppTemplateStore) -> None:
        self._store = store

    async def register(self, *, tenant: str, name: str, language: str, category: str,
                       body: str = "", variables: list[str] | None = None) -> WhatsAppTemplate:
        if not tenant or not name or not language:
            raise TemplateError("tenant, name and language are required")
        cat = (category or "").strip().upper()
        if cat not in CATEGORIES:
            raise TemplateError(f"category must be one of {CATEGORIES}, got {category!r}")
        vs = [str(v) for v in (variables or [])]
        existing = await self._store.get(tenant, name, language)
        t = WhatsAppTemplate(
            id=existing.id if existing else uuid.uuid4().hex,
            tenant=tenant, name=name, language=language, category=cat, body=body, variables=vs,
            status=PENDING, created=existing.created if existing else _now())
        t.updated = _now()
        return await self._store.save(t)

    async def get(self, tenant: str, name: str, language: str) -> WhatsAppTemplate | None:
        return await self._store.get(tenant, name, language)

    async def list(self, tenant: str, *, status: str = "", limit: int = 100,
                   after: str | None = None) -> tuple[list[dict], str | None]:
        return await self._store.find(tenant=tenant, status=status, limit=limit, after=after)

    # ── lifecycle (54.3): submit to Meta + reconcile status ─────────────────────
    async def submit(self, *, tenant: str, name: str, language: str, manager) -> WhatsAppTemplate:
        """Hand a registered template to Meta for review; record its id + returned status."""
        t = await self._store.get(tenant, name, language)
        if t is None:
            raise TemplateError("no such template to submit")
        res = manager.submit(name=t.name, language=t.language, category=t.category, body=t.body,
                             variables=t.variables)
        if res.provider_template_id:
            t.provider_template_id = res.provider_template_id
        t.status = res.status or PENDING
        t.rejection_reason = res.reason if res.status == REJECTED else ""
        t.updated = _now()
        return await self._store.save(t)

    async def sync(self, *, tenant: str, manager, limit: int = 200) -> dict[str, int]:
        """Reconcile every still-pending template for this tenant against Meta's verdict.

        Tenant-scoped filtered read (no scan). A status CHANGE is a durable fact written down, never
        a bus hint — the operator sees approved/rejected without asking Meta again."""
        rows, _ = await self._store.find(tenant=tenant, status=PENDING, limit=limit)
        updated = 0
        for row in rows:
            t = WhatsAppTemplate.from_dict(row)
            res = manager.status(provider_template_id=t.provider_template_id, name=t.name,
                                 language=t.language)
            if res.status and res.status != t.status:
                t.status = res.status
                t.rejection_reason = res.reason if res.status == REJECTED else ""
                t.updated = _now()
                await self._store.save(t)
                updated += 1
        return {"scanned": len(rows), "updated": updated}
