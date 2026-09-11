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
                             PENDING, REFUSED, SCHEDULED, VERIFIED, Dsar, new_id)
from app.core.stores import DsarStore, HoldStore, TargetStore


class DsarService:
    def __init__(self, *, dsars: DsarStore, holds: HoldStore, targets: TargetStore,
                 orchestrator, clock, config: dict[str, Any], event_bus=None,
                 dispatcher=None) -> None:
        self._dsars = dsars
        self._holds = holds
        self._targets = targets
        self._orch = orchestrator
        self._clock = clock
        self._cfg = config
        self._bus = event_bus
        self._dispatch = dispatcher

    # ── create (idempotent) ─────────────────────────────────────────────────────
    async def create(self, *, tenant: str, subject: str, type: str,
                     payload: dict | None = None, subject_kind: str = "") -> tuple[Dsar, bool]:
        existing = await self._dsars.open_for(tenant, subject, type)
        if existing is not None:
            return existing, False                    # never start a second erasure

        now = self._clock.now()
        d = Dsar(id=new_id("dsar_"), tenant=tenant, subject=subject, subject_kind=subject_kind,
                 type=type, status=PENDING,
                 payload=payload or {}, created_at=now,
                 deadline_at=now + self._cfg["sla_days"] * 86400,          # UTC
                 verification_token=secrets.token_urlsafe(32),
                 verification_expires=now + self._cfg["verification_ttl"])
        await self._dsars.save(d)
        await self._emit("dsar.created", {"id": d.id, "type": type, "subject": subject})
        return d, True

    # ── identity verification (before ANY PII leaves) ───────────────────────────
    async def verify(self, dsar_id: str, token: str, tenant: str = "") -> dict[str, Any]:
        d = await self._dsars.get(dsar_id)
        # `tenant`, when supplied (always from the API), scopes ownership so one tenant's admin can
        # never act on another's DSAR (IDOR). "" = a trusted internal caller (the sweeper), unscoped.
        if d is None or (tenant and d.tenant != tenant):
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
    async def process(self, dsar_id: str, tenant: str = "") -> dict[str, Any]:
        d = await self._dsars.get(dsar_id)
        if d is None or (tenant and d.tenant != tenant):     # API-supplied tenant scopes ownership
            return {"ok": False, "reason": "NOT_FOUND"}
        if d.status in (COMPLETED, REFUSED):
            return {"ok": True, "status": d.status, "reason": "already_final"}

        # (3) identity before PII — access/export release personal data
        if d.type in (ACCESS, EXPORT) and not d.verified:
            return {"ok": False, "reason": "NOT_VERIFIED"}

        # (1) legal hold beats erasure. Re-checked on EVERY process() — including when the sweep
        # re-enters after the grace window — so a hold placed during the cooling-off still refuses.
        if d.type == ERASURE:
            holds = await self._holds.active_for(d.tenant, d.subject)
            if holds:
                d.status = REFUSED
                d.refusal_reason = f"legal_hold:{holds[0].id}"
                await self._dsars.save(d)
                await self._emit("dsar.refused", {"id": d.id, "reason": "legal_hold"})
                return {"ok": False, "status": REFUSED, "reason": "LEGAL_HOLD",
                        "hold_id": holds[0].id}

            # (1b) GRACE — cooling-off before any target is told to erase, so an admin can place a
            # hold first (checked directly above). ERASURE_GRACE_DAYS=0 → off, dispatch immediately
            # (today's behaviour). Scheduled on the first process after verification; the sweep
            # re-enters here once due and falls through to dispatch. Erasure only — access/export/
            # correction never reach this branch. Independent of the SLA deadline by design.
            grace = int(self._cfg.get("erasure_grace_days", 0) or 0)
            if grace > 0:
                now = self._clock.now()
                if not d.dispatch_at:
                    d.dispatch_at = now + grace * 86400
                    d.status = SCHEDULED
                    await self._dsars.save(d)
                    await self._emit("dsar.scheduled",
                                     {"id": d.id, "dispatch_at": d.dispatch_at})
                    return {"ok": True, "status": SCHEDULED, "dispatch_at": d.dispatch_at}
                if now < d.dispatch_at:
                    return {"ok": True, "status": SCHEDULED, "dispatch_at": d.dispatch_at,
                            "reason": "GRACE_PENDING"}
                # due → fall through to build targets + dispatch

        if d.type == CORRECTION:
            # correction is applied by the owning services; still fanned out like erasure
            pass

        # fan out one task per data-map target; completion is tracked per target
        targets = await self._targets.for_tenant(d.tenant)
        if not targets:
            return {"ok": False, "reason": "NO_DATA_TARGETS_REGISTERED"}

        # setdefault, NOT rebuild: a second /process on an in-flight request must not reset acks
        # already received. It used to be an unconditional `d.targets = {key: False ...}`, which wiped
        # every acknowledgement — and since completion is DERIVED from these acks, the request could no
        # longer complete at all while the 30-day statutory clock ran. New targets start unacked; a
        # target that already acked keeps its True.
        if d.targets is None:
            d.targets = {}
        for t in targets:
            d.targets.setdefault(f"{t.service}:{t.category}", False)

        # SAVE THE ATTESTATION MAP BEFORE ANYONE IS TOLD TO ERASE. The map used to be dispatched first
        # and saved after: a target that erased fast and pushed its ack back found no map yet and was
        # refused UNKNOWN_TARGET, and the later in-memory save overwrote any ack that HAD landed — so a
        # deletion that really happened never reached `completed` and went overdue at day 30, which is
        # what a regulator reads. Persisting first means an early ack finds its key and is kept.
        d.status = IN_PROGRESS
        await self._dsars.save(d)

        dispatched = 0
        for t in targets:
            ok = await self._orch.dispatch(request_id=d.id, action=d.type, tenant=d.tenant,
                                           subject=d.subject, target=t.to_dict())
            dispatched += 1 if ok else 0
        # Re-read before recording the dispatched flag: an ack may have landed in the store DURING
        # dispatch, and a blind save of the stale in-memory copy would clobber it. The map is already
        # persisted, so this only stamps metadata onto the current row.
        d = await self._dsars.get(d.id) or d
        d.dispatched = dispatched == len(targets)
        await self._dsars.save(d)
        await self._emit("dsar.dispatched", {"id": d.id, "targets": len(targets),
                                             "dispatched": dispatched})

        # Drive the fan-out NOW rather than waiting for the next sweep: the subject's clock is
        # already running. The sweep is the safety net for whatever does not ack on this pass, not
        # the primary path — an erasure that only starts on a cron tick is an erasure that has not
        # started. A dispatch failure here is not fatal: the targets stay unacked, which is exactly
        # the state the sweeper knows how to resume from.
        result = None
        if self._dispatch is not None:
            try:
                result = await self._dispatch.drive(d)
            except Exception:
                pass

        return {"ok": True, "status": d.status, "targets": len(targets),
                "dispatched": dispatched, "fully_dispatched": d.dispatched,
                "fanout": result}

    # ── a target confirms it erased/exported its slice ──────────────────────────
    async def ack_target(self, dsar_id: str, service: str, category: str,
                         ack: dict | None = None) -> dict[str, Any]:
        d = await self._dsars.get(dsar_id)
        if d is None:
            return {"ok": False, "reason": "NOT_FOUND"}
        key = f"{service}:{category}"
        if key not in d.targets:
            return {"ok": False, "reason": "UNKNOWN_TARGET"}
        d.targets[key] = True                                 # idempotent
        if ack is not None:
            # Keep the evidence beside the flag: WHAT was erased and what was kept, with basis.
            state = dict(d.target_state or {})
            st = dict(state.get(key, {}))
            st.update(status="acked", ack=ack, next_at=0.0, last_at=self._clock.now())
            state[key] = st
            d.target_state = state
        # (2) completion is DERIVED from the acks — never declared. Same rule the dispatcher
        # applies, called from the model so the two paths cannot drift apart again.
        if d.derive_completion(self._clock.now()):
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

    async def list_all_open(self, limit: int = 100,
                            after: str | None = None) -> tuple[list[dict], str | None]:
        """Every tenant's still-OPEN DSAR, oldest-first — the platform compliance-officer view (a
        PDPL response deadline spans shops, and a request must be discoverable without already knowing
        which shop it came from). Reuses the sweeper's indexed, pushed-down open-status find (never a
        raw scan), paginated. Platform-scoped: gated to platform.compliance at the route + audited."""
        now = self._clock.now()
        dsars, cursor = await self._dsars.open_dsars(limit=limit, after=after)
        out = []
        for d in dsars:
            item = d.public()
            item["overdue"] = d.overdue(now)
            out.append(item)
        return out, cursor

    async def _emit(self, event: str, payload: dict) -> None:
        if self._bus is None:
            return
        try:
            await self._bus.publish(event, payload)
        except Exception:
            pass
