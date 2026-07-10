"""Data-subject requests (PDPL/GDPR): access, export, erasure, correction.

The four rules that make this legally correct rather than merely functional:

  1. **Legal hold beats erasure.** If the subject is under an active hold (litigation,
     regulatory investigation, tax retention), erasure is REFUSED. Deleting held data is
     the violation — losing it is worse than keeping it.
  2. **Completion is derived, never declared.** An erasure spans every service holding the
     subject's data (the data map). The request is `completed` only when EVERY target has
     acknowledged. A partial delete reported as "done" is a breach of the subject's rights.
  3. **Identity before PII.** An access/export request must be verified with a token before
     any personal data is released — otherwise the erasure/export endpoint becomes the best
     data-exfiltration tool an attacker ever had.
  4. **Deadlines in UTC.** The SLA clock (PDPL/GDPR ~30 days) is computed in UTC and the
     request is flagged `overdue` past it.

Requests are idempotent: re-submitting the same open request returns it rather than
starting a second erasure.
"""
from __future__ import annotations

import hmac
import secrets
from typing import Any

from app.core.models import (ACCESS, COMPLETED, CORRECTION, ERASURE, EXPORT, IN_PROGRESS,
                             PENDING, REFUSED, VERIFIED, Dsar, new_id)
from app.core.stores import DsarStore, HoldStore, TargetStore


class DsarService:
    def __init__(self, *, dsars: DsarStore, holds: HoldStore, targets: TargetStore,
                 orchestrator, clock, config: dict[str, Any], event_bus=None) -> None:
        self._dsars = dsars
        self._holds = holds
        self._targets = targets
        self._orch = orchestrator
        self._clock = clock
        self._cfg = config
        self._bus = event_bus

    # ── create (idempotent) ─────────────────────────────────────────────────────
    async def create(self, *, tenant: str, subject: str, type: str,
                     payload: dict | None = None) -> tuple[Dsar, bool]:
        existing = await self._dsars.open_for(tenant, subject, type)
        if existing is not None:
            return existing, False                    # never start a second erasure

        now = self._clock.now()
        d = Dsar(id=new_id("dsar_"), tenant=tenant, subject=subject, type=type, status=PENDING,
                 payload=payload or {}, created_at=now,
                 deadline_at=now + self._cfg["sla_days"] * 86400,          # UTC
                 verification_token=secrets.token_urlsafe(32),
                 verification_expires=now + self._cfg["verification_ttl"])
        await self._dsars.save(d)
        await self._emit("dsar.created", {"id": d.id, "type": type, "subject": subject})
        return d, True

    # ── identity verification (before ANY PII leaves) ───────────────────────────
    async def verify(self, dsar_id: str, token: str) -> dict[str, Any]:
        d = await self._dsars.get(dsar_id)
        if d is None:
            return {"ok": False, "reason": "NOT_FOUND"}
        now = self._clock.now()
        if d.verification_expires and now > d.verification_expires:
            return {"ok": False, "reason": "TOKEN_EXPIRED"}
        if not d.verification_token or not hmac.compare_digest(d.verification_token, token or ""):
            return {"ok": False, "reason": "BAD_TOKEN"}       # constant-time
        d.verified = True
        d.status = VERIFIED if d.status == PENDING else d.status
        await self._dsars.save(d)
        return {"ok": True, "verified": True}

    # ── process ──────────────────────────────────────────────────────────────────
    async def process(self, dsar_id: str) -> dict[str, Any]:
        d = await self._dsars.get(dsar_id)
        if d is None:
            return {"ok": False, "reason": "NOT_FOUND"}
        if d.status in (COMPLETED, REFUSED):
            return {"ok": True, "status": d.status, "reason": "already_final"}

        # (3) identity before PII — access/export release personal data
        if d.type in (ACCESS, EXPORT) and not d.verified:
            return {"ok": False, "reason": "NOT_VERIFIED"}

        # (1) legal hold beats erasure
        if d.type == ERASURE:
            holds = await self._holds.active_for(d.tenant, d.subject)
            if holds:
                d.status = REFUSED
                d.refusal_reason = f"legal_hold:{holds[0].id}"
                await self._dsars.save(d)
                await self._emit("dsar.refused", {"id": d.id, "reason": "legal_hold"})
                return {"ok": False, "status": REFUSED, "reason": "LEGAL_HOLD",
                        "hold_id": holds[0].id}

        if d.type == CORRECTION:
            # correction is applied by the owning services; still fanned out like erasure
            pass

        # fan out one task per data-map target; completion is tracked per target
        targets = await self._targets.for_tenant(d.tenant)
        if not targets:
            return {"ok": False, "reason": "NO_DATA_TARGETS_REGISTERED"}

        d.targets = {f"{t.service}:{t.category}": False for t in targets}
        dispatched = 0
        for t in targets:
            ok = await self._orch.dispatch(request_id=d.id, action=d.type, tenant=d.tenant,
                                           subject=d.subject, target=t.to_dict())
            dispatched += 1 if ok else 0
        d.dispatched = dispatched == len(targets)
        d.status = IN_PROGRESS
        await self._dsars.save(d)
        await self._emit("dsar.dispatched", {"id": d.id, "targets": len(targets),
                                             "dispatched": dispatched})
        return {"ok": True, "status": d.status, "targets": len(targets),
                "dispatched": dispatched, "fully_dispatched": d.dispatched}

    # ── a target confirms it erased/exported its slice ──────────────────────────
    async def ack_target(self, dsar_id: str, service: str, category: str) -> dict[str, Any]:
        d = await self._dsars.get(dsar_id)
        if d is None:
            return {"ok": False, "reason": "NOT_FOUND"}
        key = f"{service}:{category}"
        if key not in d.targets:
            return {"ok": False, "reason": "UNKNOWN_TARGET"}
        d.targets[key] = True                                 # idempotent
        # (2) completion is DERIVED from the acks — never declared
        if d.is_complete():
            d.status = COMPLETED
            d.completed_at = self._clock.now()
            await self._emit("dsar.completed", {"id": d.id, "type": d.type})
        await self._dsars.save(d)
        return {"ok": True, "status": d.status, "remaining": [k for k, v in d.targets.items() if not v]}

    async def get(self, dsar_id: str) -> Dsar | None:
        return await self._dsars.get(dsar_id)

    async def list(self, tenant: str) -> list[dict]:
        now = self._clock.now()
        out = []
        for row in await self._dsars.list(tenant, self._cfg["list_limit"]):
            d = Dsar.from_dict(row)
            item = d.public()
            item["overdue"] = d.overdue(now)
            out.append(item)
        return out

    async def _emit(self, event: str, payload: dict) -> None:
        if self._bus is None:
            return
        try:
            await self._bus.publish(event, payload)
        except Exception:
            pass
