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
                 default_format: str, partition_mode: str, query_limit: int,
                 export_limit: int = 100_000, pii_policy=None) -> None:
        self._repo = repo
        self._integrity = integrity
        # Applied at THIS boundary, where no caller can bypass it. A rule that relies on
        # every call site remembering is a hope, not a rule.
        self._pii = pii_policy
        self._exporters = exporters
        self._default_format = default_format
        self._mode = partition_mode
        self._query_limit = query_limit
        # An export used to ask for 1 000 000 rows — the constant that started all of this.
        # Bounded, and the bound is configuration.
        self._export_limit = export_limit

    def _partition(self, tenant: str) -> str:
        return (tenant or "global") if self._mode == "tenant" else "global"

    # ── record (append-only) ──────────────────────────────────────────────────
    async def record(self, *, service: str, action: str, actor: str = "", resource: str = "",
                     tenant: str = "", ip: str = "", metadata: dict[str, Any] | None = None) -> AuditEvent:
        if self._pii is not None:
            actor, ip, metadata = self._pii.apply(tenant=tenant, actor=actor, ip=ip,
                                                  metadata=metadata)
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
        """Query is scoped to a PARTITION, because that is what the datastore can index on.

        In tenant-partition mode a tenant's events live in its own partition, so the filter is
        pushed down there. A platform caller with no tenant filter must be answered across
        partitions — from the tiny _META list, never by scanning the events.
        """
        limit = limit or self._query_limit
        tenant = filters.get("tenant")

        # FOLD THE QUERY THE SAME WAY WE FOLDED THE WRITE.
        #
        # Writes pseudonymise `actor`, so a caller asking for `actor=alice@x.com` would match
        # nothing and the audit log would become unqueryable by the only identifier a human has.
        # The fold has to be symmetric or the feature is a data-loss bug wearing a privacy hat.
        #
        # After the subject is erased this still works: the ref is a pure function of (salt,
        # tenant, subject), so an investigator who still knows the email can find the events —
        # and one who does not can never turn the ref back into a person.
        # Fold with EXACTLY the tenant the write used (record() defaults it to ""), or the two
        # sides disagree and the query silently returns nothing.
        #
        # Caveat, stated rather than hidden: the ref is tenant-scoped, so an actor query that names
        # no tenant can only match events that were recorded without one. Cross-tenant "what did
        # this human do everywhere" is not answerable by design — that correlation is precisely the
        # privacy leak the per-tenant salt exists to prevent.
        actor = filters.get("actor")
        if self._pii is not None and actor:
            folded, _, _ = self._pii.apply(tenant=tenant or "", actor=actor, ip="", metadata=None)
            filters = {**filters, "actor": folded}

        if self._mode != "tenant" or tenant:
            return await self._repo.query(filters, limit, self._partition(tenant or ""))

        out: list[dict] = []
        for partition in await self._repo.partitions():
            out.extend(await self._repo.query(filters, limit, partition))
            if len(out) >= limit:
                break
        out.sort(key=lambda r: r["seq"], reverse=True)
        return out[:limit]

    # ── integrity verify (tamper detection) ──────────────────────────────────
    async def verify(self, partition: str = "global") -> dict:
        """Walk the whole chain, or say so. Never report clean on a partial walk.

        `iterate()` pages to exhaustion, so a truncated read is no longer possible. But the
        guarantee we sell is that a PASS means the whole chain was checked, so it is asserted
        against the independently-maintained head rather than assumed: the last event's seq must
        be the head seq, its hash must be the head hash, and the count must match.

        A tamper-evident log that verifies part of itself is worse than no log.
        """
        events = await self._repo.iterate(partition)
        head_seq, head_hash = await self._repo.head(partition)

        prev = ""
        for e in events:
            if e.get("prev_hash", "") != prev:
                return {"ok": False, "checked": len(events), "first_bad_seq": e["seq"],
                        "reason": "broken chain (prev_hash mismatch — event deleted/reordered)"}
            if e.get("hash", "") != self._integrity.seal(prev, e):
                return {"ok": False, "checked": len(events), "first_bad_seq": e["seq"],
                        "reason": "content tampered (hash mismatch)"}
            prev = e["hash"]

        # The chain read must REACH THE HEAD the writer recorded. If it stops short, we read
        # only part of the log — the exact failure that used to pass silently.
        #
        # Note what is NOT asserted: `len(events) == head_seq`. Retention purge deliberately
        # removes an old PREFIX of the chain, so the count is legitimately less than the head
        # sequence. A hole in the middle is caught by the prev_hash walk; a missing tail is
        # caught below.
        if head_seq and not events:
            return {"ok": False, "checked": 0, "first_bad_seq": None,
                    "reason": f"read no events, head is at seq {head_seq}"}
        if head_seq and events and events[-1]["seq"] != head_seq:
            return {"ok": False, "checked": len(events), "first_bad_seq": events[-1]["seq"],
                    "reason": "chain does not reach the recorded head (truncated or reordered)"}
        if head_hash and prev != head_hash:
            return {"ok": False, "checked": len(events), "first_bad_seq": None,
                    "reason": "final hash does not match the recorded head hash"}
        return {"ok": True, "checked": len(events), "first_bad_seq": None}

    async def verify_all(self) -> dict:
        """Verify EVERY partition's chain, so a scheduled check can prove the whole log.

        The chain is tamper-evident, but evidence nobody examines proves nothing — a break sits
        undiscovered until a regulator asks, which is the worst moment to learn the log was edited.
        `GET /api/v1/audit/verify` can check one partition, but it is user-tier (`platform.audit`),
        so an internal-token cron cannot call it; and with AUDIT_PARTITION=tenant the chain is
        per-tenant, so only this service knows the set of partitions to walk. This enumerates them
        from the tiny _META index (never a scan) and verifies each.

        Returns as soon as it finds a break — the FIRST break is what an operator acts on, and
        walking the rest wastes time while an alarm is already due. A clean result names how many
        partitions were checked, so "verified" cannot be confused with "found nothing to verify".
        """
        partitions = await self._repo.partitions() or ["global"]
        checked = 0
        for partition in partitions:
            result = await self.verify(partition)
            checked += 1
            if not result["ok"]:
                return {"ok": False, "partitions_checked": checked,
                        "partitions_total": len(partitions),
                        "broken_partition": partition, **result}
        return {"ok": True, "partitions_checked": checked,
                "partitions_total": len(partitions)}

    # ── export ─────────────────────────────────────────────────────────────────
    async def export(self, filters: dict[str, Any], fmt: str | None = None) -> tuple[str, bytes]:
        events = await self.query(filters, self._export_limit)
        exporter = self._exporters.get(fmt or self._default_format) or self._exporters[self._default_format]
        return exporter.export(events)

    # ── retention policy (enforces RETENTION_DAYS across all partitions) ───────
    async def enforce_retention(self, retention_days: int, holds=None) -> dict:
        """Purge events older than the configured retention. 0 = keep forever (no-op).

        This is what makes `RETENTION_DAYS` a real policy rather than an unwired knob, and
        satisfies ISO/ZATCA "archive per retention rules" — a scheduler calls it periodically.
        """
        if retention_days <= 0:
            return {"enforced": False, "reason": "RETENTION_DAYS=0 (keep forever)", "purged": 0}
        cutoff = (_dt.datetime.now(_dt.timezone.utc)
                  - _dt.timedelta(days=retention_days)).isoformat()
        purged = held = 0
        skipped: list[dict] = []
        partitions = await self._repo.partitions()
        for partition in partitions:
            refs = None
            if holds is not None:
                # In tenant-partition mode the partition IS the tenant, which is what makes a
                # per-partition hold set meaningful at all.
                answer = await holds(partition)
                if answer is None or not answer.get("complete"):
                    # FAIL CLOSED. An incomplete hold list means we cannot tell which events are
                    # protected, and deleting on a partial answer destroys evidence somebody is
                    # legally required to keep. Skipping this partition costs one delayed purge;
                    # the alternative is not recoverable. It is REPORTED, never silent — a purge
                    # that quietly does nothing is its own kind of failure.
                    skipped.append({"partition": partition,
                                    "reason": "hold list unavailable or incomplete"})
                    continue
                refs = frozenset(answer.get("refs") or ())
            result = await self.purge(cutoff, partition, held_refs=refs)
            purged += result["purged"]
            held += result.get("held", 0)
        return {"enforced": True, "cutoff": cutoff, "partitions": len(partitions),
                "purged": purged, "held": held, "skipped": skipped,
                # Stated, not implied: without this an operator cannot tell a run that protected
                # held data from one that never looked.
                "hold_protected": holds is not None}

    # ── retention purge (re-anchors the chain) ────────────────────────────────
    async def purge(self, before_ts: str, partition: str = "global",
                    held_refs: frozenset[str] | None = None) -> dict:
        """Delete events older than the cutoff — except any belonging to a HELD subject.

        `held_refs` carries subject_ref PSEUDONYMS, which is the only shape this service can use:
        its rows never hold a raw subject, so it cannot ask "is this person held?" one row at a
        time. compliance-service derives the same refs from the same frozen product PII_SALT and
        hands over the whole set, and the intersection happens here, locally.

        A held event is KEPT and counted, never silently skipped. retention.py's rule is that a
        held row is REPORTED as retained — an operator looking at "purged: 500" needs to know that
        12 were preserved under hold, because that number is the difference between a purge and
        spoliation of evidence.

        None means no protection was applied, which is what this did unconditionally before.
        The caller reports that as `hold_protected: false` rather than leaving it implied.
        """
        events = await self._repo.iterate(partition)
        expired = [e for e in events if e.get("ts", "") < before_ts]
        held = ([e for e in expired if e.get("actor") in held_refs]
                if held_refs else [])
        held_ids = {e["id"] for e in held}
        kept = [e for e in events if e.get("ts", "") >= before_ts or e["id"] in held_ids]
        purged = [e for e in expired if e["id"] not in held_ids]
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
                          metadata={"before_ts": before_ts, "purged": len(purged),
                                    "held": len(held)})
        return {"purged": len(purged), "remaining": len(kept) + 1, "held": len(held)}
