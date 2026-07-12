"""Erasure — authz-service: what it deletes for a data subject, and what it keeps, with the reason.

Role assignments name the person. They are deleted outright.
Roles and policies themselves are tenant configuration, not personal data — they describe
what a role may do, not who anybody is.

Every ack names what was ERASED and what was RETAINED **with its legal basis**. "We kept it" is
always an answer with a reason attached, never a silence — a DSAR that cannot say why data
survived has not really answered the person.

Idempotent: a DSAR retries, so erasing an already-erased subject is a no-op that still acks.
"""
from __future__ import annotations

from typing import Any

from app.core.pii import subject_ref


_ASSIGN = "assignments"


class AuthzErasure:
    service = "authz-service"
    category = "access_grants"

    def __init__(self, *, store, pii_salt: str) -> None:
        self._store = store
        self._salt = pii_salt

    async def erase(self, tenant: str, subject: str) -> dict[str, Any]:
        ref = subject_ref(self._salt, tenant, subject)
        deleted = 0
        cursor = None
        while True:
            rows, cursor = await self._store.find(_ASSIGN, {"tenant": tenant, "subject": subject},
                                                  limit=500, after=cursor, sort=("created", "asc"))
            for r in rows:
                await self._store.delete(_ASSIGN, r.get("id") or f"{tenant}::{subject}")
                deleted += 1
            if not cursor:
                break

        return {
            "service": self.service,
            "subject_ref": ref,
            "erased": [f"assignments:{deleted}"] if deleted else [],
            "retained": [],   # roles/policies are tenant config, not personal data
            "already_absent": deleted == 0,
        }
