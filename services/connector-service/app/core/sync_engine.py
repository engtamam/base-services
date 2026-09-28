"""Sync engine — incremental pull/push with the guarantees that make a sync trustworthy.

  • **The watermark advances only after the batch is durably processed.** Advancing it first
    (the obvious implementation) permanently skips every record in a batch that then fails —
    silent, unrecoverable data loss. Here the cursor is written last, and only on success.
  • **Per-record idempotency.** Every record carries a stable key (connection + resource +
    source id). A retried batch re-processes nothing it already committed, so an at-least-once
    retry cannot double-write.
  • **Partial failure is reported as partial.** 997 ok / 3 failed ⇒ the 997 stand, the 3 are
    dead-lettered, and the run is `partial`. Never "success"; never a rollback of good work.
  • **Echo-loop guard.** A record we just pulled from the provider is tagged with its origin;
    a push never sends back what the provider gave us (A→B→A loops eat rate limits and
    corrupt data).
  • **Retry-After + circuit breaker.** The provider tells us when to come back; a dead
    provider stops consuming workers.
"""
from __future__ import annotations
import asyncio
import hashlib
import json

from typing import Any

from app.core import backoff as _backoff
from app.core import circuit as _circuit
from app.core import mapping as _mapping
from app.core.models import Connection, SyncRecord, SyncRun, new_id
from app.core.stores import ConnectionStore, DlqStore, RunStore, SeenStore
from app.ports.connector_provider import (CredentialInvalid, ProviderUnavailable, RateLimited)

ORIGIN_FIELD = "_origin"        # set on records we pulled; never pushed back


class SyncEngine:
    def __init__(self, *, connections: ConnectionStore, runs: RunStore, dlq: DlqStore,
                 seen: SeenStore, tokens, provider, clock, config: dict[str, Any],
                 event_bus=None) -> None:
        self._conns = connections
        self._runs = runs
        self._dlq = dlq
        self._seen = seen
        self._tokens = tokens
        self._provider = provider
        self._clock = clock
        self._cfg = config
        self._bus = event_bus

    def _idem_key(self, conn_id: str, resource: str, record: dict) -> str:
        source_id = record.get("id") or record.get("vid") or record.get("_id")
        if not source_id:
            # No natural source id — hash the record CONTENT so distinct id-less records get distinct
            # keys. The old `or ''` collapsed every id-less record to one key `conn:res:`, so the
            # first was processed and all the rest were silently seen-skipped (dropped). The hash is
            # stable across retries of the same record (same content), preserving idempotency.
            digest = hashlib.sha256(
                json.dumps(record, sort_keys=True, separators=(",", ":"), default=str).encode()
            ).hexdigest()
            source_id = f"h:{digest[:32]}"
        return f"{conn_id}:{resource}:{source_id}"

    async def _guard_circuit(self, conn: Connection) -> str:
        state = _circuit.state_now(circuit_state=conn.circuit_state, opened_at=conn.opened_at,
                                   now=self._clock.now(),
                                   reset_seconds=self._cfg["circuit_reset_seconds"])
        return state

    async def _record_failure(self, conn: Connection, state: str, error: str) -> None:
        # CAS-mutate (connector-003): re-read + re-apply so a concurrent run's watermark/circuit update
        # is not clobbered by this failure write.
        def _apply(c: Connection) -> None:
            c.failure_count += 1
            c.last_error = error[:256]
            for k, v in _circuit.on_failure(consecutive_failures=c.consecutive_failures,
                                            state=state, now=self._clock.now(),
                                            threshold=self._cfg["circuit_threshold"]).items():
                setattr(c, k, v)
        await self._conns.mutate(conn.id, _apply)

    async def _record_success(self, conn: Connection) -> None:
        def _apply(c: Connection) -> None:
            c.sync_count += 1
            c.last_sync_at = self._clock.now()
            for k, v in _circuit.on_success().items():
                setattr(c, k, v)
        await self._conns.mutate(conn.id, _apply)

    # ── pull ────────────────────────────────────────────────────────────────────
    async def pull(self, *, connection_id: str, resource: str, spec: dict[str, Any],
                   field_map: dict | None = None, ack_cursor: str = "",
                   at_least_once: bool = False) -> dict[str, Any]:
        """Incremental pull.

        DELIVERY MODES (audit-07):
          • Default (at_least_once=False): the watermark advances as soon as the batch is fetched and
            returned. Correct for a consumer that writes into a durable, deduplicating target — but if
            the HTTP response is lost in transit the caller never sees the batch, and the advanced
            watermark means it is never re-fetched (at-most-once → silent loss on a dropped response).
          • at_least_once=True: the watermark is NOT advanced by this call. The caller durably processes
            the returned records (deduping on the returned idempotency `keys`) and, on its NEXT pull,
            passes `ack_cursor` = the `cursor` it received here — only then does the watermark advance.
            A lost response means no ack, so the same batch is re-delivered: at-least-once, no loss.
        """
        conn = await self._conns.get(connection_id)
        if conn is None:
            return {"ok": False, "reason": "NOT_FOUND"}

        state = await self._guard_circuit(conn)
        if not _circuit.can_attempt(state):
            return {"ok": False, "reason": "circuit_open"}

        # ACK the previously delivered batch. The ack is VALIDATED against the token this service
        # actually issued for the pending batch (conn.pending) — an arbitrary caller-supplied cursor is
        # no longer trusted to jump the watermark to any string (which would skip/lose records — r2-03).
        # A terminal batch (provider next_cursor == "") is unackable by cursor, so we issued a synthetic
        # non-empty token for it; acking it RESERVES the delivered keys so the tail stops re-delivering
        # forever (r2-01), rather than advancing the watermark to "" (which means "from the start").
        pend = conn.pending.get(resource) if isinstance(conn.pending, dict) else None
        if ack_cursor and pend and ack_cursor == pend.get("token"):
            if pend.get("terminal"):
                for k in pend.get("keys", []):
                    await self._seen.reserve(k)        # commit the tail: future fetches skip these

            def _ack(c: Connection) -> None:           # CAS-mutate (connector-003)
                p = c.pending.get(resource) if isinstance(c.pending, dict) else None
                if not (p and p.get("token") == ack_cursor):
                    return                             # already acked by a concurrent run
                if not p.get("terminal"):
                    c.watermarks[resource] = p.get("cursor", "")
                c.pending.pop(resource, None)
            await self._conns.mutate(connection_id, _ack)
            # Refresh the local aggregate: the rest of this pull reads conn.watermarks to decide where
            # to fetch from, and the CAS mutate advanced the DURABLE row, not this stale local copy.
            conn = await self._conns.get(connection_id) or conn

        now = self._clock.now()
        run = SyncRun(id=new_id("run_"), tenant=conn.tenant, connection_id=conn.id,
                      resource=resource, direction="pull", started_at=now,
                      watermark_before=conn.watermarks.get(resource, ""))
        await self._runs.save(run)

        try:
            access = await self._tokens.credentials(conn.tenant, conn.id, spec)
            page = await asyncio.to_thread(                  # blocking provider HTTP OFF the event loop
                self._provider.fetch, access_token=access, resource=resource,
                cursor=run.watermark_before, limit=self._cfg["batch_size"], spec=spec)
        except RateLimited as exc:
            await self._record_failure(conn, state, "rate_limited")
            delay = _backoff.next_delay(0, base_seconds=self._cfg["retry_base_seconds"],
                                        max_seconds=self._cfg["retry_max_seconds"],
                                        max_attempts=self._cfg["max_attempts"], seed=conn.id,
                                        jitter=self._cfg["retry_jitter"],
                                        retry_after=exc.retry_after)
            return await self._finish(run, "failed", error="rate_limited",
                                      extra={"retry_in": delay})
        except (ProviderUnavailable, CredentialInvalid) as exc:
            await self._record_failure(conn, state, type(exc).__name__)
            return await self._finish(run, "failed", error=type(exc).__name__)

        mapped, failed = [], 0
        for rec in page.records:
            key = self._idem_key(conn.id, resource, rec)
            # In at-least-once mode a batch is RE-DELIVERED until acked, so a delivery reservation
            # (which would skip a record on the retry) must NOT gate it — the consumer dedupes on the
            # returned key. In the default mode the reserve is the atomic at-most-once gate (was
            # seen()->mark(), a check-then-set two concurrent runs could both pass).
            if not at_least_once:
                if not await self._seen.reserve(key):
                    continue                              # already committed on a prior/concurrent run
            elif await self._seen.seen(key):
                # alo: a key is only reserved once it is COMMITTED — a durably dead-lettered poison
                # record (r2-02) or an acked terminal-tail record (r2-01). Skip it so it is neither
                # re-DLQ'd nor re-delivered on every subsequent poll.
                continue
            try:
                out = _mapping.apply(field_map or {}, rec) if field_map else dict(rec)
                out[ORIGIN_FIELD] = conn.provider          # echo-loop marker
                mapped.append((key, out))
            except Exception as exc:
                failed += 1
                # Reserve the poison key so a re-fetch (retry, or an at-least-once re-delivery before
                # ack) does not re-DLQ the same unmappable record every time.
                if at_least_once:
                    await self._seen.reserve(key)
                await self._dlq.add(SyncRecord(
                    id=new_id("dlq_"), tenant=conn.tenant, connection_id=conn.id, run_id=run.id,
                    resource=resource, idempotency_key=key, record=rec,
                    error=f"map:{type(exc).__name__}", created=self._clock.now()))
                # reservation intentionally KEPT: the record is durably dead-lettered, so it must not
                # be re-fetched and re-DLQ'd on every subsequent run (the poison-pill stall).

        run.records_in = len(page.records)
        run.records_out = len(mapped)
        run.records_failed = failed

        # ── watermark advancement ────────────────────────────────────────────────
        # Default mode: advance whenever the provider gave a next cursor (an unmappable record is
        # durably DLQ'd AND reserved, so advancing cannot lose it, and NOT advancing froze progress
        # forever — the poison-pill stall). At-least-once mode: DO NOT advance here — the caller acks
        # the returned cursor on its next pull, and only that advances the watermark (deliver-then-mark).
        next_cursor = page.next_cursor or ""
        if at_least_once:
            run.watermark_after = run.watermark_before     # unchanged until the caller acks
            # Record the UNACKED delivery so the next pull's ack can be validated (r2-03) and a
            # terminal batch (empty provider cursor) is still ackable via a synthetic non-empty token
            # (r2-01). token is the value the caller echoes; cursor is the real position to advance to.
            terminal = not next_cursor
            # Deterministic terminal token (no run id): a re-pull of the same terminal page yields the
            # SAME token, so acking the token from any delivery of that page commits it.
            token = next_cursor if next_cursor else f"__eos__:{run.watermark_before}"
            _pending_val = {"token": token, "cursor": next_cursor,
                            "terminal": terminal, "keys": [k for k, _ in mapped]}

            def _adv(c: Connection) -> None:           # CAS-mutate (connector-003)
                if isinstance(c.pending, dict):
                    c.pending[resource] = _pending_val
            await self._conns.mutate(connection_id, _adv)
        elif next_cursor:
            run.watermark_after = next_cursor

            def _adv(c: Connection) -> None:           # CAS-mutate (connector-003)
                c.watermarks[resource] = next_cursor
            await self._conns.mutate(connection_id, _adv)
        else:
            run.watermark_after = run.watermark_before

        # The PROVIDER fetch succeeded, so the connection is healthy — a client-side field-map failure
        # is a tenant config error, not a provider outage, and must NOT trip the provider circuit
        # breaker (which would disable a healthy connection). Record success regardless of map failures.
        await self._record_success(conn)
        status = "succeeded" if failed == 0 else "partial"
        out = await self._finish(run, status)
        out["records"] = [r for _k, r in mapped]
        # The token to ACK on the next pull (at-least-once), and the per-record idempotency keys the
        # consumer dedupes on across a re-delivery. In alo mode the cursor is the ack TOKEN (a synthetic
        # non-empty value for a terminal page, so the tail is ackable — r2-01); in default mode it is the
        # provider cursor. Returned in both modes; only ack mode requires them.
        if at_least_once:
            # Use the token we just computed + persisted via the CAS mutate — the local conn.pending is
            # a stale copy (the mutate advanced the durable row, not this object).
            out["cursor"] = token
        else:
            out["cursor"] = next_cursor
        out["keys"] = [k for k, _r in mapped]
        return out

    # ── push ────────────────────────────────────────────────────────────────────
    async def push(self, *, connection_id: str, resource: str, records: list[dict],
                   spec: dict[str, Any], field_map: dict | None = None) -> dict[str, Any]:
        conn = await self._conns.get(connection_id)
        if conn is None:
            return {"ok": False, "reason": "NOT_FOUND"}
        state = await self._guard_circuit(conn)
        if not _circuit.can_attempt(state):
            return {"ok": False, "reason": "circuit_open"}

        run = SyncRun(id=new_id("run_"), tenant=conn.tenant, connection_id=conn.id,
                      resource=resource, direction="push", started_at=self._clock.now())
        await self._runs.save(run)

        try:
            access = await self._tokens.credentials(conn.tenant, conn.id, spec)
        except CredentialInvalid as exc:
            await self._record_failure(conn, state, "credential_invalid")
            return await self._finish(run, "failed", error=type(exc).__name__)

        sent = failed = skipped = 0
        for rec in records:
            if rec.get(ORIGIN_FIELD) == conn.provider:
                skipped += 1                              # echo-loop guard: don't send it back
                continue
            key = self._idem_key(conn.id, resource, rec)
            # ATOMIC reserve BEFORE the send, so two concurrent runs cannot both push the same record.
            # A reservation that then FAILS to send is released, so a retry can resend (no lost write).
            if not await self._seen.reserve(key):
                skipped += 1                              # already pushed / being pushed
                continue
            payload = _mapping.apply(field_map or {}, rec) if field_map else {
                k: v for k, v in rec.items() if k != ORIGIN_FIELD}
            try:
                # OFFLOAD the blocking provider HTTP off the event loop (urllib inside push()); calling
                # it inline froze the single-worker loop for the whole round-trip, per record — the
                # same crash-loop class pull() already avoids with to_thread.
                await asyncio.to_thread(
                    self._provider.push, access_token=access, resource=resource, record=payload,
                    idempotency_key=key, spec=spec)
                sent += 1                                 # reservation stands = committed
            except (ProviderUnavailable, RateLimited, CredentialInvalid) as exc:
                await self._seen.release(key)             # send failed → free it so a retry resends
                failed += 1
                await self._dlq.add(SyncRecord(
                    id=new_id("dlq_"), tenant=conn.tenant, connection_id=conn.id, run_id=run.id,
                    resource=resource, idempotency_key=key, record=rec,
                    error=type(exc).__name__, created=self._clock.now()))

        run.records_in = len(records)
        run.records_out = sent
        run.records_failed = failed
        if failed:
            await self._record_failure(conn, state, "push_failures")
        else:
            await self._record_success(conn)
        status = "succeeded" if failed == 0 else ("failed" if sent == 0 else "partial")
        out = await self._finish(run, status)
        out["skipped"] = skipped
        return out

    async def _finish(self, run: SyncRun, status: str, *, error: str = "",
                      extra: dict | None = None) -> dict[str, Any]:
        run.status = status
        run.error = error
        run.finished_at = self._clock.now()
        await self._runs.save(run)
        await self._emit(f"sync.{status}", {"run_id": run.id, "resource": run.resource})
        return {"ok": status in ("succeeded", "partial"), **run.to_dict(), **(extra or {})}

    async def health(self, connection_id: str) -> dict[str, Any] | None:
        conn = await self._conns.get(connection_id)
        if conn is None:
            return None
        total = conn.sync_count + conn.failure_count
        return {"connection_id": conn.id, "provider": conn.provider, "status": conn.status,
                "circuit_state": await self._guard_circuit(conn),
                "sync_count": conn.sync_count, "failure_count": conn.failure_count,
                "error_rate": (conn.failure_count / total) if total else 0.0,
                "last_error": conn.last_error, "last_sync_at": conn.last_sync_at,
                "watermarks": conn.watermarks}

    async def _emit(self, event: str, payload: dict) -> None:
        if self._bus is None:
            return
        try:
            await self._bus.publish(event, payload)
        except Exception:
            pass
