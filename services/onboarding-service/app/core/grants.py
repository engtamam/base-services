"""Issuing a grant, once — and only to someone entitled to it.

This is where the welcome gift lives, and it is the part of onboarding an attacker actually cares
about. Both reference implementations studied could be drained the same way: sign up, collect the
credit, delete the account, sign up again. Their uniqueness key was the tenant or the email, and
both are free to create.

Three defences, applied in order, each closing a hole the previous one leaves open.

**1. once_per names a VERIFIED identity, not a tenant.** A tenant can be deleted and remade; a
verified commercial registration cannot be re-minted. verification-service keeps that hash through
erasure precisely so this check still works after an account is deleted.

**2. An unverified claim of uniqueness is refused, not trusted.** If a flow says
`once_per: verified_identity` and no verification exists, the grant does NOT fall through to
"allow" — that would make the strongest control in the design depend on the customer choosing to
verify. It refuses and says what is missing.

**3. The key is semantic and the backend is idempotent.** Two concurrent requests, a retry after a
timeout, a replayed webhook: same key, one grant. Idempotency at the transport layer alone would
still double-grant across a restart.

The failure mode this whole file avoids: reporting a grant that did not happen. A customer told
they have credit plans around it and discovers the truth at the till, which is worse than being
told the grant failed.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from app.ports.entitlement import ALREADY, FAILED, GRANTED, REFUSED, GrantRequest, Receipt

# What a once_per key may be scoped to, worst to best:
#   tenant            free to create — a deleted tenant resets it. Farmable.
#   subject           free to create with another email. Farmable.
#   verified_identity survives deletion, and costs a real registration to obtain.
ONCE_PER_TENANT = "tenant"
ONCE_PER_SUBJECT = "subject"
ONCE_PER_VERIFIED = "verified_identity"
WEAK_KEYS = (ONCE_PER_TENANT, ONCE_PER_SUBJECT)


@dataclass
class GrantOutcome:
    status: str
    reference: str = ""
    detail: str = ""

    @property
    def granted(self) -> bool:
        return self.status in (GRANTED, ALREADY)


class GrantService:
    def __init__(self, *, entitlement, ledger, identity_lookup=None, specs: dict | None = None,
                 local_facts=None, auditor=None) -> None:
        self._entitlement = entitlement
        self._ledger = ledger                      # our record of what we have issued
        self._identity = identity_lookup           # verification-service: the durable identity key
        self._specs = specs or {}                  # grant kind -> amount/days/currency, from config
        # The local record that makes the REQUIREMENT read as satisfied. Without it the grant is
        # delivered and the flow still shows it outstanding, so the customer is asked to claim a
        # gift they already have.
        self._local = local_facts
        self._auditor = auditor

    async def issue(self, *, kind: str, once_per: str, tenant: str, subject: str,
                    claim_ref: str = "", requirement: str = "",
                    scope: str = "tenant") -> GrantOutcome:
        """Issue `kind` at most once per `once_per`."""
        spec = self._specs.get(kind)
        if spec is None:
            # A flow granting something the platform never defined. Refused loudly: silently
            # granting nothing would let a flow complete while the customer got none of what the
            # product promised them.
            return GrantOutcome(status=REFUSED,
                                detail=f"no grant spec configured for {kind!r}")

        key = await self._uniqueness_key(once_per, tenant, subject, claim_ref)
        if key is None:
            return GrantOutcome(
                status=REFUSED,
                detail=(f"{kind!r} is granted once per {once_per!r}, and no verified identity is "
                        f"on record for this subject. The grant is withheld rather than issued on "
                        f"an unverified claim of uniqueness."))

        # Our own ledger first: it is the record that survives whatever the backend does, and it is
        # what makes a second attempt cheap rather than a round trip to wallet-service.
        existing = await self._ledger.find(key)
        if existing:
            return GrantOutcome(status=ALREADY, reference=existing.get("reference", ""),
                                detail="already issued for this identity")

        request = GrantRequest(
            kind=kind, tenant=tenant, subject=subject, idempotency_key=key,
            amount=int(spec.get("amount", 0)), currency=str(spec.get("currency", "")),
            days=int(spec.get("days", 0)), metadata={"once_per": once_per},
        )
        try:
            receipt = await self._entitlement.issue(request)
        except Exception as exc:
            # The port forbids raising; an adapter is ordinary code. A bug must not look like a
            # delivered grant.
            receipt = Receipt(status=FAILED, detail=f"entitlement adapter raised: {exc}")

        if not receipt.granted:
            # NOT recorded in the ledger. Recording a failed grant would permanently block the
            # retry that would have succeeded — the customer would be owed a gift the system
            # believed it had already given.
            return GrantOutcome(status=FAILED, detail=receipt.detail)

        await self._ledger.record(key, kind=kind, tenant=tenant, subject=subject,
                                  reference=receipt.reference)
        if self._local is not None:
            await self._local.mark_granted(requirement=requirement or kind, tenant=tenant,
                                           subject=subject, scope=scope,
                                           reference=receipt.reference)
        if self._auditor is not None:
            await self._auditor.record(
                "grant.issued", actor=subject, tenant=tenant, resource=f"grant:{kind}",
                metadata={"once_per": once_per, "reference": receipt.reference})
        return GrantOutcome(status=GRANTED, reference=receipt.reference)

    async def _uniqueness_key(self, once_per: str, tenant: str, subject: str,
                              claim_ref: str) -> str | None:
        """The string that must be unique. None means we cannot establish uniqueness — refuse.

        For verified_identity the key is derived from verification-service's claim_ref, which is
        already a salted one-way hash and already survives erasure. We hash again with the grant
        scope so this service's ledger cannot be used to test whether a given identity exists
        elsewhere on the platform.
        """
        if once_per == ONCE_PER_VERIFIED:
            ref = claim_ref or await self._lookup_identity(tenant, subject)
            if not ref:
                return None
            return "vid:" + hashlib.sha256(f"grant|{ref}".encode()).hexdigest()[:32]
        if once_per == ONCE_PER_TENANT:
            return f"tenant:{tenant}"
        if once_per == ONCE_PER_SUBJECT:
            return f"subject:{tenant}:{subject}"
        return None

    async def _lookup_identity(self, tenant: str, subject: str) -> str:
        if self._identity is None:
            return ""
        try:
            return await self._identity.claim_ref_for(tenant, subject) or ""
        except Exception:
            # Unreachable verification-service means we cannot establish uniqueness. Refusing is
            # the safe direction: a delayed gift is recoverable, a duplicated one is not.
            return ""
