"""Erasure — tenant: organisations, not people, and one field that needs watching.

`Tenant` holds a slug, a name, domains and a status. A tenant NAME is a company; a domain is a
company's. None of it identifies a natural person, so there is nothing here to delete — the
membership between a person and a tenant lives in identity-service, which is where erasing it
actually breaks the link.

The endpoint exists so a DSAR can PROVE it asked. "tenant-service was never contacted" and
"tenant-service holds nothing" are indistinguishable in an incomplete system and completely
different to a regulator.

One real exception, and it is why this file is not empty: `branding` is a free-form dict documented
as holding an `email_sender`. That is an address, and an address can be a named individual's rather
than a role account. So branding IS walked, and a value matching the subject is removed — the rest
of the branding (logo, colours) is untouched, because a tenant's visual identity is not the person.
"""
from __future__ import annotations

from typing import Any

from app.core.pii import subject_ref

_TENANTS = "tenants"


class TenantErasure:
    service = "tenant-service"
    category = "tenant_records"

    # tenant-006: erase() reads ONE tenant row by id and scrubs its branding — there is no scan here, so
    # the old ERASURE_MAX_SCAN/_PAGE/max_scan knob was advertised but never read. Removed rather than left
    # as a lie about a bound that does not exist.
    def __init__(self, *, store, pii_salt: str) -> None:
        self._store = store
        self._salt = pii_salt

    async def erase(self, tenant: str, subject: str) -> dict[str, Any]:
        ref = subject_ref(self._salt, tenant, subject)
        cleared = 0

        row = await self._store.get(_TENANTS, tenant)
        if row:
            branding = dict(row.get("branding") or {})
            hits = [k for k, v in branding.items() if isinstance(v, str) and v == subject]
            for k in hits:
                # The person leaves; the tenant's visual identity stays.
                branding[k] = ""
            if hits:
                row["branding"] = branding
                # atomic: ok — DSAR erasure of one subject's branding strings; rare, idempotent
                # (re-erasing finds no hits), and not a concurrent request path.
                await self._store.put(_TENANTS, tenant, row)
                cleared = len(hits)

        return {
            "service": self.service,
            "category": self.category,
            "subject_ref": ref,
            "erased": [f"tenant_branding_contact:{cleared}"] if cleared else [],
            "retained": [],
            "already_absent": cleared == 0,
            "note": ("tenant records describe organisations, not natural persons; the person-to-"
                     "tenant membership lives in identity-service, where erasing it breaks the "
                     "link. Only branding contact addresses are subject data here."),
        }
