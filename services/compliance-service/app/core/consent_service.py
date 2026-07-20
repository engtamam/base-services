"""Consent — grant, withdraw, and the cached `check` that gates all personal-data processing.

Legal properties encoded here:
  • **Default DENY.** No consent record ⇒ not allowed. Consent is never assumed; an unknown
    subject/purpose is a refusal, not a permission.
  • **Purpose limitation.** Consent is stored per (subject, purpose). Consent for marketing
    grants nothing for analytics.
  • **Withdrawal is immediate.** Any grant/withdraw bumps a per-tenant cache generation, so
    a cached "allowed" cannot outlive the withdrawal.
  • **Expiry.** A consent past `expires_at` is inactive.
  • **Immutable proof.** Every grant/withdraw appends a ConsentProof — the record an auditor
    (or SDAIA) asks for. Proofs are never mutated or deleted.

HOT PATH: `check` runs before every processing operation, so it is cached and the cache is
invalidated by generation, not by TTL alone.
"""
from __future__ import annotations

import json
from typing import Any

from app.core.capacity import CapacityPolicy
from app.core.models import Consent, ConsentProof, SELF, new_id
from app.core.stores import ConsentStore

GRANTED, WITHDRAWN = "granted", "withdrawn"


class ConsentService:
    def __init__(self, *, store: ConsentStore, clock, cache=None, cache_ttl: int = 30,
                 list_limit: int = 100, event_bus=None,
                 capacity: CapacityPolicy | None = None) -> None:
        self._store = store
        self._clock = clock
        self._cache = cache
        self._ttl = cache_ttl
        self._limit = list_limit
        self._bus = event_bus
        # Default policy = not enforced, which is what every deployment before Phase 39 was
        # implicitly asserting. Making it an object rather than a flag keeps the decision in one
        # place instead of scattered across the grant path.
        self._capacity = capacity or CapacityPolicy()

    # ── generation-keyed cache ─────────────────────────────────────────────────
    async def _gen(self, tenant: str) -> str:
        if self._cache is None:
            return "0"
        try:
            return (await self._cache.get(f"consentgen:{tenant}")) or "0"
        except Exception:
            return "0"

    async def _bump(self, tenant: str) -> None:
        if self._cache is None:
            return
        try:
            g = await self._cache.get(f"consentgen:{tenant}")
            await self._cache.set(f"consentgen:{tenant}", str(int(g or 0) + 1), None)
        except Exception:
            pass

    async def _cache_get(self, key: str):
        if self._cache is None:
            return None
        try:
            raw = await self._cache.get(key)
            return json.loads(raw) if raw else None
        except Exception:
            return None

    async def _cache_set(self, key: str, value) -> None:
        if self._cache is None:
            return
        try:
            await self._cache.set(key, json.dumps(value), self._ttl)
        except Exception:
            pass

    # ── grant / withdraw ────────────────────────────────────────────────────────
    async def grant(self, *, tenant: str, subject: str, purpose: str, notice_version: str = "",
                    method: str = "", expires_at: float = 0.0, actor: str = "", ip: str = "",
                    basis: str = SELF, consented_by: str = "", relation: str = "",
                    subject_born_at: float = 0.0) -> Consent:
        now = self._clock.now()

        # Capacity is checked BEFORE anything is written. A refusal must leave no consent row and
        # no proof: a proof recording a consent we refused to accept would be the same lie in a
        # different direction.
        lapses_at = self._capacity.check(basis=basis, consented_by=consented_by,
                                         born_at=subject_born_at, now=now)
        # Guardian consent lapses at majority. If the caller also set an expiry, the EARLIER wins —
        # a shorter consent must not be silently extended by the capacity rule.
        if lapses_at:
            expires_at = min(expires_at, lapses_at) if expires_at else lapses_at

        c = Consent(tenant=tenant, subject=subject, purpose=purpose, granted=True,
                    notice_version=notice_version, method=method, expires_at=expires_at,
                    updated_at=now, basis=basis, consented_by=consented_by, relation=relation,
                    subject_born_at=subject_born_at)
        await self._store.put(c)
        await self._store.add_proof(ConsentProof(
            id=new_id("prf_"), tenant=tenant, subject=subject, purpose=purpose, action=GRANTED,
            notice_version=notice_version, method=method, actor=actor, ip=ip, at=now,
            basis=basis, consented_by=consented_by, relation=relation))
        await self._bump(tenant)                      # a stale 'denied' must not persist
        await self._emit("consent.granted", {"subject": subject, "purpose": purpose})
        return c

    async def withdraw(self, *, tenant: str, subject: str, purpose: str, actor: str = "",
                       ip: str = "") -> Consent:
        now = self._clock.now()
        existing = await self._store.get(tenant, subject, purpose)
        c = existing or Consent(tenant=tenant, subject=subject, purpose=purpose)
        c.granted = False
        c.updated_at = now
        await self._store.put(c)
        await self._store.add_proof(ConsentProof(
            id=new_id("prf_"), tenant=tenant, subject=subject, purpose=purpose, action=WITHDRAWN,
            notice_version=c.notice_version, actor=actor, ip=ip, at=now))
        await self._bump(tenant)                      # withdrawal takes effect IMMEDIATELY
        await self._emit("consent.withdrawn", {"subject": subject, "purpose": purpose})
        return c

    # ── check (HOT PATH) ────────────────────────────────────────────────────────
    async def check(self, *, tenant: str, subject: str, purpose: str) -> dict[str, Any]:
        gen = await self._gen(tenant)
        # Normalised, or `Matching` and `matching` would occupy separate cache entries and a
        # withdrawal recorded under one spelling would leave the other still answering "allowed"
        # until its TTL expired. The store key is normalised; a cache key that is not would
        # reintroduce the split one layer above the fix.
        purpose = self._store.normalise(purpose)
        key = f"consent:{gen}:{tenant}:{subject}:{purpose}"
        cached = await self._cache_get(key)
        if cached is not None:
            return {**cached, "cached": True}

        now = self._clock.now()
        c = await self._store.get(tenant, subject, purpose)
        if c is None:
            result = {"allowed": False, "reason": "no_consent_record"}      # DEFAULT DENY
        else:
            # CAPACITY IS RE-EVALUATED HERE, not merely trusted from write time. grant() folds a
            # lapse date into expires_at, which covers every consent granted since the policy
            # existed — and covers NOTHING granted before it, because those rows carry
            # expires_at=0 and no amount of waiting changes a stored zero. Deriving the majority
            # date on read means a lapse is a fact about the subject's age rather than a value
            # somebody remembered to write down.
            majority = self._majority_for(c)
            status = c.status_at(now, majority)
            if status == "granted":
                result = {"allowed": True, "reason": "granted",
                          "notice_version": c.notice_version}
            else:
                result = {"allowed": False, "reason": status}
        result["purpose"] = purpose
        await self._cache_set(key, result)
        return {**result, "cached": False}

    def _majority_for(self, c) -> float | None:
        """The day this consent's subject can speak for themself, or None if unknowable.

        None means "we cannot evaluate capacity" — no policy configured, or no birth date on the
        record — and it is NOT the same as "no lapse". status_at() treats None as un-evaluated and
        falls back to the stored expiry, so an unknown age never manufactures permission and never
        manufactures a refusal either.
        """
        if self._capacity is None or not self._capacity.enforced:
            return None
        if not c.subject_born_at:
            return None
        return self._capacity.majority_at(c.subject_born_at)

    async def history(self, tenant: str, subject: str) -> dict[str, Any]:
        return {"consents": await self._store.for_subject(tenant, subject, self._limit),
                "proofs": await self._store.proofs(tenant, subject, self._limit)}

    async def _emit(self, event: str, payload: dict) -> None:
        if self._bus is None:
            return
        try:
            await self._bus.publish(event, payload)
        except Exception:
            pass
