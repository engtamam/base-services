"""Erasure — identity-service owns the LINKAGE, so erasing here is what makes a person unlinkable.

Every immutable record in the platform (the audit hash-chain, the money ledgers, the tax invoice)
stores a `subject_ref` pseudonym, never a name or an email. The mapping from that pseudonym back
to a human exists in exactly ONE place: this service's user row.

So identity does not "help with" an erasure — identity IS the erasure. Delete the user row and its
lookup index, and every `sub_…` scattered across the immutable records becomes an orphan that no
longer points at anybody. The audit chain still verifies; the invoice is still filed; neither can
name the person any more.

This service therefore RETAINS NOTHING. There is no tax basis, no security basis, no proof of
consent that lives here. Everything it holds is the identity itself, and the identity is the thing
being erased.

Idempotent by construction: erasing a subject that is already gone is a no-op that still returns a
receipt, because a DSAR retries and must not fail the second time.
"""
from __future__ import annotations

from typing import Any

from app.core.pii import subject_ref


class IdentityErasure:
    """Erase a subject's identity and destroy the pseudonym linkage."""

    service = "identity-service"
    category = "identity"

    def __init__(self, *, users, refresh, store, pii_salt: str) -> None:
        self._users = users
        self._refresh = refresh
        self._store = store
        self._salt = pii_salt

    async def erase(self, tenant: str, subject: str) -> dict[str, Any]:
        """Delete the user, the lookup index, and every refresh token. Retain nothing.

        Returns the receipt compliance-service records as the ack: what was erased, and what was
        retained WITH ITS LEGAL BASIS. "We kept it" is always an answer with a reason attached —
        never a silence.
        """
        ref = subject_ref(self._salt, tenant, subject)
        erased: list[str] = []

        user = await self._users.get_by_username(tenant, subject)
        if user is not None:
            # The refresh tokens go first. If the process dies between the two writes, a user row
            # with no valid session is safe; a live session for a deleted user is not.
            revoked = await self._refresh.revoke_family(user.id)
            if revoked:
                erased.append(f"refresh_tokens:{revoked}")
            await self._users.delete(user)
            erased.append("user")
            erased.append("username_index")     # the linkage itself

        return {
            "service": self.service,
            "subject_ref": ref,
            "erased": erased,
            "retained": [],          # identity keeps NOTHING: it is the thing being erased
            "already_absent": user is None,
        }
