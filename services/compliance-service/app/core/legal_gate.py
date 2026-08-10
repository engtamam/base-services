"""Post-auth access gate — which required legal documents must this person (re-)accept before they
may proceed, and the accept that clears it.

The re-acceptance problem is what makes this more than a checkbox: when the terms change, everyone
who accepted the OLD version must accept the NEW one, and the system must know the difference. The
insight that keeps this small is that acceptance IS a consent — of a specific NOTICE VERSION — so
this reuses the consent machinery whole rather than inventing a parallel store:

  • accepting doc D at version V  ==  a consent grant for purpose "legal:D" with notice_version=V,
  • "have they accepted the CURRENT version?"  ==  a consent check whose granted notice_version
    equals the doc's current published version,
  • a version bump makes every prior acceptance stale automatically, because the granted
    notice_version no longer matches — no sweep, no migration, no flag to flip.

The gate is DEFAULT-DENY in the sense that matters: a required document with no matching current-
version acceptance is OUTSTANDING. It reads the same current pointer the publish path writes, so a
freshly published version is enforced on the very next check.
"""
from __future__ import annotations

from typing import Any


class LegalGate:
    def __init__(self, *, legal_store, consents, clock) -> None:
        self._legal = legal_store          # LegalPageStore
        self._consents = consents          # ConsentService
        self._clock = clock

    @staticmethod
    def _purpose(doc_id: str) -> str:
        return f"legal:{doc_id}"

    async def outstanding(self, *, tenant: str, subject: str,
                          locale: str = "") -> list[dict[str, Any]]:
        """Every required document this subject has NOT accepted at its current version. Empty means
        they are clear to proceed. Each entry carries enough to render the prompt: the current
        version and its viewer payload."""
        out: list[dict[str, Any]] = []
        for ptr in await self._legal.documents(tenant):
            if not ptr.get("requires_acceptance"):
                continue                    # informational page — shown, never gated on
            doc_id, current = ptr["doc_id"], ptr.get("version", "")
            check = await self._consents.check(
                tenant=tenant, subject=subject, purpose=self._purpose(doc_id))
            accepted_version = check.get("notice_version") if check.get("allowed") else None
            if accepted_version == current and current:
                continue                    # accepted the version that is in force — clear
            doc = await self._legal.current(tenant, doc_id)
            entry = {"doc_id": doc_id, "current_version": current,
                     "accepted_version": accepted_version,
                     "reason": "version_changed" if accepted_version else "never_accepted"}
            if doc is not None:
                entry["view"] = doc.view(locale)
            out.append(entry)
        return out

    async def accept(self, *, tenant: str, subject: str, doc_id: str, method: str = "",
                     actor: str = "", ip: str = "") -> dict[str, Any]:
        """Record acceptance of a document's CURRENT published version. Refuses if the document is
        not published or does not require acceptance — you cannot accept nothing, and accepting a
        version that is not current would clear a gate the person has not actually satisfied.

        Acceptance is pinned to the version in force AT THIS MOMENT: a publish that lands a split
        second later leaves this acceptance stale, which is correct — they agreed to what they saw."""
        ptr = await self._legal.current_pointer(tenant, doc_id)
        if not ptr or not ptr.get("version"):
            raise LegalGateError(f"{doc_id} has no published version to accept")
        if not ptr.get("requires_acceptance"):
            raise LegalGateError(f"{doc_id} does not require acceptance")
        version = ptr["version"]
        await self._consents.grant(
            tenant=tenant, subject=subject, purpose=self._purpose(doc_id),
            notice_version=version, method=method or "legal_acceptance", actor=actor, ip=ip)
        return {"doc_id": doc_id, "accepted_version": version}


class LegalGateError(Exception):
    """Accepting a document that is unpublished or not gated on — a request that cannot be honoured."""
