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

from app.core.models import Consent, ConsentProof, new_id
from app.core.stores import ConsentStore

GRANTED, WITHDRAWN = "granted", "withdrawn"


class ConsentService:
    def __init__(self, *, store: ConsentStore, clock, cache=None, cache_ttl: int = 30,
                 list_limit: int = 100, event_bus=None) -> None:
        self._store = store
        self._clock = clock
        self._cache = cache
        self._ttl = cache_ttl
        self._limit = list_limit
        self._bus = event_bus

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
                    method: str = "", expires_at: float = 0.0, actor: str = "", ip: str = "") -> Consent:
        now = self._clock.now()
        c = Consent(tenant=tenant, subject=subject, purpose=purpose, granted=True,
                    notice_version=notice_version, method=method, expires_at=expires_at,
                    updated_at=now)
        await self._store.put(c)
        await self._store.add_proof(ConsentProof(
            id=new_id("prf_"), tenant=tenant, subject=subject, purpose=purpose, action=GRANTED,
            notice_version=notice_version, method=method, actor=actor, ip=ip, at=now))
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
        key = f"consent:{gen}:{tenant}:{subject}:{purpose}"
        cached = await self._cache_get(key)
        if cached is not None:
            return {**cached, "cached": True}

        now = self._clock.now()
        c = await self._store.get(tenant, subject, purpose)
        if c is None:
            result = {"allowed": False, "reason": "no_consent_record"}      # DEFAULT DENY
        elif not c.granted:
            result = {"allowed": False, "reason": "withdrawn"}
        elif c.expires_at and now >= c.expires_at:
            result = {"allowed": False, "reason": "expired"}
        else:
            result = {"allowed": True, "reason": "granted",
                      "notice_version": c.notice_version}
        result["purpose"] = purpose
        await self._cache_set(key, result)
        return {**result, "cached": False}

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
