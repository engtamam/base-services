"""Legal holds, RoPA (records of processing), the data map, and the breach register.

The breach clock is the part regulators actually check: PDPL/GDPR require notification
within a fixed window of *discovery* (72 hours by default). We compute the deadline in UTC
at registration, expose `hours_remaining`, and flag `overdue` — a breach that quietly runs
out of clock is the failure mode this exists to prevent.
"""
from __future__ import annotations

from typing import Any

from app.core.models import Breach, DataTarget, LegalHold, ProcessingActivity, new_id
from app.core.stores import BreachStore, HoldStore, RopaStore, TargetStore

LAWFUL_BASES = ("consent", "contract", "legal_obligation", "vital_interests",
                "public_task", "legitimate_interests")


class ComplianceService:
    def __init__(self, *, holds: HoldStore, ropa: RopaStore, breaches: BreachStore,
                 targets: TargetStore, clock, config: dict[str, Any], event_bus=None) -> None:
        self._holds = holds
        self._ropa = ropa
        self._breaches = breaches
        self._targets = targets
        self._clock = clock
        self._cfg = config
        self._bus = event_bus

    # ── legal hold ──────────────────────────────────────────────────────────────
    async def place_hold(self, *, tenant: str, subject: str, reason: str) -> LegalHold:
        h = LegalHold(id=new_id("hold_"), tenant=tenant, subject=subject, reason=reason,
                      active=True, placed_at=self._clock.now())
        await self._holds.save(h)
        await self._emit("legal_hold.placed", {"subject": subject, "hold_id": h.id})
        return h

    async def release_hold(self, hold_id: str) -> LegalHold | None:
        h = await self._holds.get(hold_id)
        if h is None:
            return None
        h.active = False
        h.released_at = self._clock.now()
        await self._holds.save(h)
        await self._emit("legal_hold.released", {"hold_id": hold_id})
        return h

    async def list_holds(self, tenant: str) -> list[dict]:
        return await self._holds.list(tenant, self._cfg["list_limit"])

    async def is_held(self, tenant: str, subject: str) -> bool:
        return bool(await self._holds.active_for(tenant, subject))

    # ── data map (drives erasure completeness) ──────────────────────────────────
    async def register_target(self, *, tenant: str, service: str, category: str,
                              description: str = "") -> DataTarget:
        t = DataTarget(id=new_id("tgt_"), tenant=tenant, service=service, category=category,
                       description=description)
        return await self._targets.register(t)

    async def list_targets(self, tenant: str) -> list[dict]:
        return [t.to_dict() for t in await self._targets.for_tenant(tenant)]

    async def delete_target(self, target_id: str) -> None:
        await self._targets.delete(target_id)

    # ── RoPA ─────────────────────────────────────────────────────────────────────
    async def create_activity(self, *, tenant: str, name: str, purpose: str, lawful_basis: str,
                              data_categories: list[str], retention_days: int = 0,
                              residency: str | None = None, cross_border: bool = False,
                              recipients: list[str] | None = None) -> ProcessingActivity:
        if lawful_basis not in LAWFUL_BASES:
            raise ValueError(f"lawful_basis must be one of {LAWFUL_BASES}")
        a = ProcessingActivity(
            id=new_id("ropa_"), tenant=tenant, name=name, purpose=purpose,
            lawful_basis=lawful_basis, data_categories=list(data_categories),
            retention_days=retention_days, residency=residency or self._cfg["residency_default"],
            cross_border=cross_border, recipients=list(recipients or []),
            created_at=self._clock.now())
        return await self._ropa.save(a)

    async def list_activities(self, tenant: str) -> list[dict]:
        return await self._ropa.list(tenant, self._cfg["list_limit"])

    async def delete_activity(self, activity_id: str) -> None:
        await self._ropa.delete(activity_id)

    # ── breach register (the 72-hour clock) ─────────────────────────────────────
    async def register_breach(self, *, tenant: str, description: str, discovered_at: float = 0.0,
                              affected_subjects: int = 0, data_categories: list[str] | None = None,
                              severity: str = "medium") -> Breach:
        now = self._clock.now()
        discovered = discovered_at or now
        b = Breach(id=new_id("brc_"), tenant=tenant, description=description,
                   discovered_at=discovered,
                   notify_deadline_at=discovered + self._cfg["breach_hours"] * 3600,   # UTC
                   affected_subjects=affected_subjects,
                   data_categories=list(data_categories or []), severity=severity)
        await self._breaches.save(b)
        await self._emit("breach.registered", {"breach_id": b.id, "deadline": b.notify_deadline_at})
        return b

    async def notify_breach(self, breach_id: str) -> dict[str, Any]:
        b = await self._breaches.get(breach_id)
        if b is None:
            return {"ok": False, "reason": "NOT_FOUND"}
        if b.notified():
            return {"ok": True, "already_notified_at": b.notified_at}
        now = self._clock.now()
        b.notified_at = now
        await self._breaches.save(b)
        late = now > b.notify_deadline_at
        await self._emit("breach.notified", {"breach_id": b.id, "late": late})
        return {"ok": True, "notified_at": now, "late": late}

    async def breach_status(self, breach_id: str) -> dict[str, Any] | None:
        b = await self._breaches.get(breach_id)
        if b is None:
            return None
        now = self._clock.now()
        return {**b.to_dict(), "notified": b.notified(), "overdue": b.overdue(now),
                "hours_remaining": round(b.hours_remaining(now), 2)}

    async def list_breaches(self, tenant: str) -> list[dict]:
        now = self._clock.now()
        out = []
        for row in await self._breaches.list(tenant, self._cfg["list_limit"]):
            b = Breach.from_dict(row)
            out.append({**b.to_dict(), "notified": b.notified(), "overdue": b.overdue(now),
                        "hours_remaining": round(b.hours_remaining(now), 2)})
        return out

    async def _emit(self, event: str, payload: dict) -> None:
        if self._bus is None:
            return
        try:
            await self._bus.publish(event, payload)
        except Exception:
            pass
