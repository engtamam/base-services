"""Legal / content pages — draft, publish, and serve a structured, versioned document.

Every platform needs the same thing and rebuilds it every time: a privacy policy, terms, a cookie
notice — authored, versioned, shown to a user, and (when it changes) re-accepted. This is that
primitive, general and config-free: a document is a slug with many versions, each version the same
instrument in every language.

TWO INVARIANTS carry the weight:
  • A PUBLISHED version is immutable. You publish a NEW version to change the wording. An acceptance
    proof (see the post-auth gate) points at the exact version agreed to; letting a published text
    change under it would turn that proof into a lie.
  • 'Current' is WRITTEN, not derived. Publishing points a per-doc pointer at the new version, so
    "what must a user have accepted?" is one read, and it does not depend on version strings sorting
    the way a human expects ("1.10" is not after "1.9" as text).

Presentation, never content: this serves STRUCTURED sections a viewer renders as its own markup, so
no page has to trust and inject stored HTML. The words themselves belong to the adopter (and, for
PDPL, to the fill-in-the-blank template pack) — not here.
"""
from __future__ import annotations

from typing import Any

from app.core.models import DRAFT, PUBLISHED, LegalDocument


class LegalError(Exception):
    """A refused authoring operation — editing a published version, publishing a missing one."""


class LegalPageService:
    def __init__(self, *, store, clock, event_bus=None) -> None:
        self._store = store
        self._clock = clock
        self._bus = event_bus

    async def draft(self, *, tenant: str, doc_id: str, version: str, content: dict[str, Any],
                    requires_acceptance: bool = False, default_locale: str = "en",
                    effective_at: float = 0.0) -> LegalDocument:
        """Create or replace a DRAFT version. Refuses to touch one already published — a published
        version is frozen, and the way to change it is to draft a new version string."""
        if not doc_id or not version:
            raise LegalError("a legal document needs a doc_id and a version")
        existing = await self._store.get_version(tenant, doc_id, version)
        if existing and existing.status == PUBLISHED:
            raise LegalError(
                f"{doc_id} {version} is already published and cannot be edited — publish a new "
                f"version instead, so anyone who accepted this one keeps a truthful record")
        doc = LegalDocument(
            tenant=tenant, doc_id=doc_id, version=version, status=DRAFT,
            requires_acceptance=requires_acceptance, content=content or {},
            default_locale=default_locale, effective_at=effective_at,
            created_at=(existing.created_at if existing else self._clock.now()))
        return await self._store.put_version(doc)

    async def publish(self, *, tenant: str, doc_id: str, version: str) -> LegalDocument:
        """Publish a drafted version and point 'current' at it. Idempotent: re-publishing the
        current version restamps nothing surprising. Publishing a NEW version is the event the
        post-auth gate turns into a re-acceptance."""
        doc = await self._store.get_version(tenant, doc_id, version)
        if doc is None:
            raise LegalError(f"cannot publish {doc_id} {version}: no such version exists")
        if doc.status != PUBLISHED:
            doc.status = PUBLISHED
            doc.published_at = self._clock.now()
            if not doc.effective_at:
                doc.effective_at = doc.published_at
            await self._store.put_version(doc)
        await self._store.set_current(tenant, doc_id, version, doc.requires_acceptance)
        await self._emit("legal.published", {"doc_id": doc_id, "version": version,
                                             "requires_acceptance": doc.requires_acceptance})
        return doc

    async def current_view(self, *, tenant: str, doc_id: str, locale: str = "") -> dict | None:
        """The viewer payload for the current published version, or None if nothing is published."""
        doc = await self._store.current(tenant, doc_id)
        return doc.view(locale) if doc else None

    async def get_view(self, *, tenant: str, doc_id: str, version: str,
                       locale: str = "") -> dict | None:
        """A specific version's viewer payload — for showing exactly what someone accepted."""
        doc = await self._store.get_version(tenant, doc_id, version)
        return doc.view(locale) if doc else None

    async def list_documents(self, *, tenant: str) -> list[dict]:
        """Each doc's current version + whether it needs acceptance — what an authoring panel reads.
        A type that only has drafts (never published) still appears, with `version: None` and
        `status: "draft"`, so a panel can resume work in progress, not just what is already live.
        The GATE never calls this — LegalGate.outstanding() reads store.documents() directly, which
        stays published-only on purpose: a draft is not yet an obligation to accept."""
        published = [{**row, "status": "published"} for row in await self._store.documents(tenant)]
        known = {row["doc_id"] for row in published}
        draft_only = [d for d in await self._store.all_doc_ids(tenant) if d not in known]
        extra = [{"doc_id": d, "version": None, "status": "draft", "requires_acceptance": False}
                 for d in draft_only]
        return published + extra

    async def history(self, *, tenant: str, doc_id: str, limit: int = 100) -> list[dict]:
        """Every version of one document, newest first — the audit trail of what was in force when."""
        rows = await self._store.versions(tenant, doc_id, limit)
        return [{"version": r.get("version"), "status": r.get("status"),
                 "published_at": r.get("published_at", 0.0),
                 "requires_acceptance": r.get("requires_acceptance", False)} for r in rows]

    async def _emit(self, event: str, payload: dict) -> None:
        if self._bus is None:
            return
        try:
            await self._bus.publish(event, payload)
        except Exception:  # noqa: BLE001 — a notice is published whether or not a listener is up
            pass
