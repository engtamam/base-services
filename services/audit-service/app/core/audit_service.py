"""Core audit logic — append-only recording with a tamper-evident chain.

record() assigns a monotonic seq and chains hash = seal(prev_hash, event). verify()
walks the chain and flags the first tampered/deleted seq. There is NO update/delete
API (append-only); retention purge is the only removal and re-anchors the chain.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

from app.core.audit_repository import AuditRepository
from app.core.models import AuditEvent, new_id, now


class AuditService:
    def __init__(self, *, repo: AuditRepository, integrity, exporters: dict,
                 default_format: str, partition_mode: str, query_limit: int) -> None:
        self._repo = repo
        self._integrity = integrity
        self._exporters = exporters
        self._default_format = default_format
        self._mode = partition_mode
        self._query_limit = query_limit

    def _partition(self, tenant: str) -> str:
        return (tenant or "global") if self._mode == "tenant" else "global"

    # ── record (append-only) ──────────────────────────────────────────────────
    async def record(self, *, service: str, action: str, actor: str = "", resource: str = "",
                     tenant: str = "", ip: str = "", metadata: dict[str, Any] | None = None) -> AuditEvent:
        partition = self._partition(tenant)
        seq, head = await self._repo.head(partition)
        evt = AuditEvent(
            id=new_id(), seq=seq + 1, ts=now(), service=service, action=action,
            actor=actor, resource=resource, tenant=tenant, ip=ip,
            metadata=metadata or {}, prev_hash=head,
        )
        evt.hash = self._integrity.seal(head, evt.to_dict())
        await self._repo.append(evt.to_dict(), partition)
        return evt

    async def query(self, filters: dict[str, Any], limit: int | None = None) -> list[dict]:
        return await self._repo.query(filters, limit or self._query_limit)

    # ── integrity verify (tamper detection) ──────────────────────────────────
    async def verify(self, partition: str = "global") -> dict:
        events = await self._repo.iterate(partition)
        prev = ""
        for e in events:
            if e.get("prev_hash", "") != prev:
                return {"ok": False, "checked": len(events), "first_bad_seq": e["seq"],
                        "reason": "broken chain (prev_hash mismatch — event deleted/reordered)"}
            if e.get("hash", "") != self._integrity.seal(prev, e):
                return {"ok": False, "checked": len(events), "first_bad_seq": e["seq"],
                        "reason": "content tampered (hash mismatch)"}
            prev = e["hash"]
        return {"ok": True, "checked": len(events), "first_bad_seq": None}

    # ── export ─────────────────────────────────────────────────────────────────
    async def export(self, filters: dict[str, Any], fmt: str | None = None) -> tuple[str, bytes]:
        events = await self.query(filters, 1_000_000)
        exporter = self._exporters.get(fmt or self._default_format) or self._exporters[self._default_format]
        return exporter.export(events)

    # ── retention policy (enforces RETENTION_DAYS across all partitions) ───────
    async def enforce_retention(self, retention_days: int) -> dict:
        """Purge events older than the configured retention. 0 = keep forever (no-op).

        This is what makes `RETENTION_DAYS` a real policy rather than an unwired knob, and
        satisfies ISO/ZATCA "archive per retention rules" — a scheduler calls it periodically.
        """
        if retention_days <= 0:
            return {"enforced": False, "reason": "RETENTION_DAYS=0 (keep forever)", "purged": 0}
        cutoff = (_dt.datetime.now(_dt.timezone.utc)
                  - _dt.timedelta(days=retention_days)).isoformat()
        purged = 0
        partitions = await self._repo.partitions()
        for partition in partitions:
            purged += (await self.purge(cutoff, partition))["purged"]
        return {"enforced": True, "cutoff": cutoff, "partitions": len(partitions), "purged": purged}

    # ── retention purge (re-anchors the chain) ────────────────────────────────
    async def purge(self, before_ts: str, partition: str = "global") -> dict:
        events = await self._repo.iterate(partition)
        kept = [e for e in events if e.get("ts", "") >= before_ts]
        purged = [e for e in events if e.get("ts", "") < before_ts]
        for e in purged:
            await self._repo.delete(e["id"])
        # re-anchor the retained events so the chain stays valid post-purge
        prev = ""
        for e in kept:
            e["prev_hash"] = prev
            e["hash"] = self._integrity.seal(prev, e)
            await self._repo.replace(e)
            prev = e["hash"]
        last_seq = kept[-1]["seq"] if kept else 0
        await self._repo.set_head(partition, last_seq, prev)
        await self.record(service="audit-service", action="audit.purged",
                          metadata={"before_ts": before_ts, "purged": len(purged)})
        return {"purged": len(purged), "remaining": len(kept) + 1}
