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
    def __init__(self, store, clock, default_force_ttl: float = 0.0) -> None:
        self._store = store
        self._clock = clock
        # onboarding-008: a force used to be PERMANENT and short-circuited the live check for ever. A
        # positive default bounds every force to a max lifetime; an expired force is treated as absent so
        # the real check resumes. 0 means "no default bound" (a caller may still pass an explicit ttl).
        self._default_ttl = float(default_force_ttl)

    async def check(self, *, fact: str, tenant: str, subject: str, req: dict) -> Answer:
        key = self._key(tenant, subject, req.get("id", fact))
        forced = await self._store.get(_FORCED, key)
        if forced:
            expires_at = float(forced.get("expires_at", 0) or 0)
            if expires_at and self._clock.now() >= expires_at:
                # onboarding-008: the force has EXPIRED — drop it and fall through to the live check,
                # instead of satisfying the requirement for ever off a stale override.
                await self._store.delete(_FORCED, key)
            else:
                return Answer(status=SATISFIED,
                              detail=f"forced by {forced.get('actor', 'an operator')}: "
                                     f"{forced.get('reason', '')}")
        if fact == "granted":
            # Scoped by the requirement's own id, so two grants in one flow do not satisfy each
            # other. Tenant-scoped grants are answered for the tenant, not the person. The DEFAULT is
            # tenant, matching issue() (spec.get('scope','tenant')) — defaulting to user here meant a
            # tenant-scoped grant was written under the tenant key but looked up under the user key,
            # so the requirement was never satisfied.
            scope = f"{tenant}:{subject}" if req.get("scope") == "user" else tenant
            row = await self._store.get(_GRANTS, f"{req.get('id', fact)}::{scope}")
            return Answer(status=SATISFIED if row else UNSATISFIED)
        return Answer(status=UNSATISFIED, detail=f"unknown local fact {fact!r}")

    async def force(self, *, tenant: str, subject: str, requirement: str, actor: str,
                    reason: str, ttl_seconds: float | None = None) -> float:
        """Mark a requirement satisfied by hand, for a BOUNDED time. ttl_seconds overrides the configured
        default; the resulting expiry is returned (0 when no bound applies). onboarding-008."""
        now = self._clock.now()
        ttl = self._default_ttl if ttl_seconds is None else float(ttl_seconds)
        expires_at = now + ttl if ttl and ttl > 0 else 0.0
        await self._store.put(_FORCED, self._key(tenant, subject, requirement), {
            "id": self._key(tenant, subject, requirement), "tenant": tenant, "subject": subject,
            "requirement": requirement, "actor": actor, "reason": reason,
            "at": now, "expires_at": expires_at})
        return expires_at

    async def unforce(self, *, tenant: str, subject: str, requirement: str) -> bool:
        """Remove a hand-force so the live check resumes immediately (onboarding-008). Returns whether a
        force existed."""
        return await self._store.delete(_FORCED, self._key(tenant, subject, requirement))

    async def mark_granted(self, *, requirement: str, tenant: str, subject: str,
                           scope: str, reference: str) -> None:
        key = f"{requirement}::{tenant if scope == 'tenant' else f'{tenant}:{subject}'}"
        # Store tenant + subject as fields so DSAR erasure's find({tenant, subject}) actually matches
        # (the row previously carried only id/reference/at, so erase matched nothing and the fact was
        # retained after erasure).
        await self._store.put(_GRANTS, key, {"id": key, "tenant": tenant, "subject": subject,
                                             "reference": reference, "at": self._clock.now()})

    @staticmethod
    def _key(tenant: str, subject: str, requirement: str) -> str:
        return f"{tenant}::{subject}::{requirement}"
