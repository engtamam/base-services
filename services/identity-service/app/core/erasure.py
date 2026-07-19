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

    def __init__(self, *, users, refresh, store, pii_salt: str, memberships=None,
                 identifier=None) -> None:
        self._users = users
        # The multi-tenant side. A Person is GLOBAL — one login across many tenants — while a DSAR
        # arrives scoped to ONE. That mismatch is the whole difficulty of this file.
        self._memberships = memberships
        self._identifier = identifier
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

        person_erased, retained = await self._erase_person(tenant, subject)
        erased.extend(person_erased)

        return {
            "service": self.service,
            "subject_ref": ref,
            "erased": erased,
            "retained": retained,
            "already_absent": user is None and not person_erased and not retained,
        }

    async def _erase_person(self, tenant: str, subject: str) -> tuple[list[str], list[dict]]:
        """The multi-tenant half, and the reason this needed a decision rather than a delete.

        A Person is one human with one login who may work for several tenants. A DSAR is made BY a
        tenant, about their relationship with that human. So:

          * The membership for THIS tenant goes. That is the relationship being ended, and it is
            what the requesting tenant actually controls.

          * If it was their LAST membership, the Person goes too — identifier, password hash, MFA
            secret, index row. Nothing is left holding a login that opens no door.

          * If they still work elsewhere, the Person is RETAINED and the receipt SAYS SO. Deleting
            it would log that human out of two other companies who never asked, on the say-so of a
            third — erasing far beyond the request, and harming the person the request exists to
            protect.

        That last case is why the old receipt was wrong rather than merely incomplete: it claimed
        `retained: []` while the most sensitive record in the platform survived, unmentioned.
        """
        erased: list[str] = []
        retained: list[dict[str, str]] = []
        if self._memberships is None or self._identifier is None:
            # IDENTITY_MULTI_TENANT is off: there are no Person rows to erase, and saying nothing
            # is correct rather than a silence.
            return erased, retained

        ident = self._identifier.display(subject)
        person = await self._memberships.person_by_identifier(ident)
        if person is None:
            return erased, retained

        await self._memberships.revoke(person.id, tenant)
        erased.append("membership")

        remaining = await self._memberships.memberships(person.id)
        others = [m for m in remaining if m.tenant != tenant]
        if others:
            retained.append({
                "what": "person",
                "basis": "other_controller_relationships",
                "why": (f"this login is one human who also works for {len(others)} other "
                        f"organisation(s). Their membership HERE is deleted, which is the "
                        f"relationship this tenant controls. Deleting the login itself would log "
                        f"them out of companies that never asked, on the say-so of one that did — "
                        f"erasing beyond the request. When their last membership ends, the person "
                        f"record and its identifier index are deleted with it."),
            })
            return erased, retained

        # Last door closed. Nothing is left holding a login that opens nothing.
        await self._refresh.revoke_family(person.id)
        await self._memberships.delete_person(person)
        erased.append("person")
        erased.append("identifier_index")
        return erased, retained
