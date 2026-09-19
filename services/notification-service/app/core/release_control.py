"""ReleaseControlService — hand WhatsApp thread control back to the Meta Business Agent after we send.

Meta's handover protocol: any message WE send to a number takes thread control away from the Business
Agent, and Meta does NOT auto-return it. For a number in `meta` handover mode (its conversations are
answered by the agent, our own assistant is off) that means every reminder/campaign we send would
leave the customer with a muted agent and nobody replying — unless we explicitly RELEASE control after
each send.

This service runs after every successful WhatsApp send: if the sending number is in `meta` mode it
durably enqueues a release, attempts it once inline (prompt), and — on failure — leaves it queued for
the drain worker to retry with backoff until it lands. A release is idempotent on Meta's side (releasing
when we no longer hold control is a harmless no-op), so retrying is always safe, and it NEVER fails
silently: an exhausted release goes `dead` (kept + logged), never dropped.

OFF by default (WHATSAPP_RELEASE_THREAD_CONTROL_ENABLED); a deployment opts in once its numbers are
actually connected to a Meta Business Agent.
"""
from __future__ import annotations

import asyncio
import time

from app.core.retry import QUEUED, RetryPolicy
from app.observability.logging import get_logger

_log = get_logger("app.release_control")

_DEAD = "dead"


class ReleaseControlService:
    def __init__(self, *, store, handover, default_mode: str, enabled: bool,
                 retry: RetryPolicy, clock=time.time) -> None:
        self._store = store
        self._handover = handover
        self._default_mode = default_mode
        self._enabled = enabled
        self._retry = retry
        self._clock = clock

    async def after_send(self, *, wa, recipient: str, tenant: str = "") -> None:
        """Called after a SUCCESSFUL WhatsApp send. Releases thread control if the sending number is in
        `meta` mode. Never raises — a release problem must never fail or unwind the send that happened.
        """
        if not self._enabled:
            return
        try:
            pid = getattr(wa, "phone_number_id", "")
            if not pid:
                return                        # no Meta number in this chain — nothing to release
            mode = (await self._handover.get(pid)) or self._default_mode
            if mode != "meta":
                return                        # `us` mode deliberately KEEPS control with us
            row = await self._store.enqueue(phone_number_id=pid, to=recipient, tenant=tenant,
                                            clock=self._clock)
            await self._attempt(wa, row)
        except Exception:  # noqa: BLE001 — durability is the queue's job; never break the send
            _log.warning("release_after_send_error", extra={"to": recipient})

    async def _attempt(self, wa, row: dict) -> bool:
        """One release attempt. Success → drop the ephemeral row. Failure → queue with backoff, or mark
        `dead` (terminal, kept as a signal) once retries are exhausted / the error is permanent."""
        attempt = int(row.get("attempts", 0)) + 1
        try:
            ok, err = await asyncio.to_thread(wa.release_thread_control, row["to"], "")
        except Exception as exc:  # noqa: BLE001
            ok, err = False, f"{type(exc).__name__}: {exc}"
        if ok:
            await self._store.delete(row["id"])      # released → drop the ephemeral row
            return True
        row["attempts"] = attempt
        row["error"] = err
        if self._retry.next_state(attempt, err) == QUEUED:
            row["status"] = QUEUED
            row["next_attempt_ts"] = self._clock() + self._retry.backoff_ms(attempt) / 1000.0
        else:
            row["status"] = _DEAD                     # FAILED or DEAD_LETTER — terminal, kept + logged
            row["next_attempt_ts"] = 0.0
            _log.error("release_thread_control_giving_up",
                       extra={"phone_number_id": row.get("phone_number_id", ""),
                              "attempts": attempt, "error": err})
        await self._store.save(row)
        return False

    async def drain(self, resolver, limit: int = 100) -> dict:
        """Re-attempt every queued release whose backoff has expired.

        `resolver(phone_number_id, tenant) -> wa | None` rebuilds the provider for the row's own number
        (same per-number credentials the send used), so a worker pod that never handled the original
        send can still release. A row whose provider cannot be resolved right now stays queued.
        """
        now = self._clock()
        due = await self._store.due(limit)
        out = {"drained": 0, "released": 0, "requeued": 0, "dead": 0}
        for row in due:
            if float(row.get("next_attempt_ts", 0) or 0) > now:
                continue                             # backing off; its turn has not come
            out["drained"] += 1
            try:
                wa = await resolver(row.get("phone_number_id", ""), row.get("tenant", ""))
            except Exception:  # noqa: BLE001
                wa = None
            if wa is None:
                out["requeued"] += 1                 # credentials unavailable now — leave it queued
                continue
            if await self._attempt(wa, row):
                out["released"] += 1
            else:
                out["dead" if row.get("status") == _DEAD else "requeued"] += 1
        return out
