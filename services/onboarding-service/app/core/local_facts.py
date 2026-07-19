"""Facts THIS service owns — which is deliberately as few as possible.

Two only:

  **granted**  did we already issue the grant this requirement carries? Ours because we are the
               service that issues it; nobody else can answer.
  **forced**   did an operator mark this satisfied by hand? Ours because it is our override.

Everything else belongs to the service that owns it and is asked for on every read. The temptation
is to cache "email verified" here for speed; the reason not to is that a cache only ever drifts
toward permissive — written when the fact becomes true, never rewritten when it stops being.

A forced fact is recorded with WHO forced it and WHY, because it is the one path that satisfies a
requirement without the underlying thing being true.
"""
from __future__ import annotations

from app.ports.requirement_source import SATISFIED, UNSATISFIED, Answer

_FORCED = "onboarding_forced"
_GRANTS = "onboarding_grants"


class LocalFacts:
    def __init__(self, store, clock) -> None:
        self._store = store
        self._clock = clock

    async def check(self, *, fact: str, tenant: str, subject: str, req: dict) -> Answer:
        forced = await self._store.get(_FORCED, self._key(tenant, subject, req.get("id", fact)))
        if forced:
            return Answer(status=SATISFIED,
                          detail=f"forced by {forced.get('actor', 'an operator')}: "
                                 f"{forced.get('reason', '')}")
        if fact == "granted":
            # Scoped by the requirement's own id, so two grants in one flow do not satisfy each
            # other. Tenant-scoped grants are answered for the tenant, not the person.
            scope = tenant if req.get("scope") == "tenant" else f"{tenant}:{subject}"
            row = await self._store.get(_GRANTS, f"{req.get('id', fact)}::{scope}")
            return Answer(status=SATISFIED if row else UNSATISFIED)
        return Answer(status=UNSATISFIED, detail=f"unknown local fact {fact!r}")

    async def force(self, *, tenant: str, subject: str, requirement: str, actor: str,
                    reason: str) -> None:
        await self._store.put(_FORCED, self._key(tenant, subject, requirement), {
            "id": self._key(tenant, subject, requirement), "tenant": tenant, "subject": subject,
            "requirement": requirement, "actor": actor, "reason": reason,
            "at": self._clock.now()})

    async def mark_granted(self, *, requirement: str, tenant: str, subject: str,
                           scope: str, reference: str) -> None:
        key = f"{requirement}::{tenant if scope == 'tenant' else f'{tenant}:{subject}'}"
        await self._store.put(_GRANTS, key, {"id": key, "reference": reference,
                                             "at": self._clock.now()})

    @staticmethod
    def _key(tenant: str, subject: str, requirement: str) -> str:
        return f"{tenant}::{subject}::{requirement}"
