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
_PAGE = 500


class TenantErasure:
    service = "tenant-service"
    category = "tenant_records"

    def __init__(self, *, store, pii_salt: str, max_scan: int = 100_000) -> None:
        self._store = store
        self._salt = pii_salt
        self._max = max_scan

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
