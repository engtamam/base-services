"""PrivacyDispatcher — drive an erasure to every target in the data map until each one has acked.

This is the piece that turns a DSAR from a record into an effect. The design rests on one insight:

    **the unacked targets ARE the queue.**

The DSAR already stores per-target ack state, because completion is DERIVED from it. That state is
durable, tenant-scoped and idempotent, so it doubles as a resumable work list — re-driving a request
simply retries whatever has not acked yet. No external queue is needed for correctness, which is what
lets this run identically in a single container, a cron sweep, or ten replicas.

Rules that are not negotiable:

  * **A target is acked only when it says so.** Unreachable, 500, a body we cannot read — all FAILED
    and retried. We never infer that a delete happened.
  * **Re-driving is safe.** Already-acked targets are skipped, so a sweep that runs twice (or two
    replicas racing) costs a no-op, not a double erasure or a lost ack.
  * **Exhaustion is loud.** After PRIVACY_MAX_ATTEMPTS a target goes FAILED and STAYS in the
    remaining list. The DSAR never reaches `completed`. A DSAR silently marked done with data still
    out there is the one outcome worse than an obviously stuck one.
  * **Backoff is full-jitter.** All targets fail together when a network partition hits; retrying them
    in lockstep would hammer the recovering side. Jitter spreads the herd.
  * **A target with no configured URL is FAILED, not skipped.** It is in the data map, so it holds
    her data; not knowing where it lives is a reason to shout, not to quietly declare success.
"""
from __future__ import annotations

import asyncio
from typing import Any

from app.ports.privacy_transport import ACKED, FAILED, PENDING

UNREGISTERED = "no PRIVACY_TARGETS entry for this service — it is in the data map but we do not know where it lives"


class PrivacyDispatcher:
    def __init__(self, *, dsars, targets, transport, settings, clock, secrets=None,
                 telemetry=None, random=None, credentials=None) -> None:
        self._dsars = dsars
        self._targets = targets
        self._transport = transport
        self._cfg = settings
        self._clock = clock
        self._secrets = secrets
        # An operator credential store, when one is wired. It is consulted FIRST and the secrets
        # port is its fallback, so a deployment that has stored nothing dispatches with exactly the
        # credential it dispatches with today.
        self._credentials = credentials
        self._telemetry = telemetry
        # injected so backoff is deterministic under test; the production default is real jitter
        self._random = random

    # ── one DSAR ────────────────────────────────────────────────────────────────
    async def drive(self, dsar) -> dict[str, Any]:
        """Attempt every target of this DSAR that is due and not yet acked.

        Returns a summary; the DSAR itself is the durable record. Safe to call repeatedly."""
        state = _state_of(dsar)
        due = [key for key in dsar.targets
               if not dsar.targets[key] and self._is_due(state.get(key, {}))]
        if not due:
            return self._summary(dsar, attempted=0)

        sem = asyncio.Semaphore(max(1, self._cfg.PRIVACY_CONCURRENCY))

        async def one(key: str) -> None:
            async with sem:
                await self._attempt(dsar, key, state)

        await asyncio.gather(*(one(k) for k in due))

        dsar.target_state = state
        # Completion stays DERIVED: recomputed from the ack map, never set by a dispatch result.
        # Applied HERE as well as on the push path, because a DSAR whose targets all answer on the
        # first dispatch would otherwise never close — every target erased, nobody marked it done.
        if dsar.derive_completion(self._clock.now()):
            self._emit_completed(dsar)
        await self._dsars.save(dsar)
        return self._summary(dsar, attempted=len(due))

    async def _attempt(self, dsar, key: str, state: dict) -> None:
        st = dict(state.get(key, {}))
        st["attempts"] = int(st.get("attempts", 0)) + 1
        service, _, _category = key.partition(":")

        url = self._url_for(service)
        if url is None:
            outcome_status, detail, ack = FAILED, UNREGISTERED, None
        else:
            out = await self._transport.erase(
                url=url, token=await self._token_for(service, dsar.tenant), request_id=dsar.id,
                action=dsar.type, tenant=dsar.tenant, subject=dsar.subject,
                timeout=self._cfg.PRIVACY_DISPATCH_TIMEOUT_SECONDS)
            outcome_status, detail, ack = out.status, out.detail, out.ack

        now = self._clock.now()
        st["status"] = outcome_status
        st["detail"] = detail
        st["last_at"] = now

        if outcome_status == ACKED:
            st["ack"] = ack
            st["next_at"] = 0.0
            dsar.targets[key] = True           # the ack map is the single source of completion
        elif outcome_status == PENDING:
            # The target owns the rest of this: it will PUSH its ack. We stop dialing so a slow
            # erasure is not restarted every sweep — but only for a BOUNDED time.
            #
            # This used to set next_at = 0.0, "never due again", and combine with a short-circuit
            # in _is_due. A target that answered 202 and then never pushed was therefore waited on
            # for ever: not acked, not failed, not exhausted, so not in `stuck` either. The DSAR
            # hung open past its statutory deadline and the only trace was `overdue` in a listing
            # nothing alerts on. A push can be lost to a crash, a deploy or a dropped queue message,
            # and none of those announce themselves.
            #
            # So a pending target is simply DUE AGAIN after PRIVACY_PENDING_TIMEOUT_SECONDS. Erase
            # is idempotent by contract — a target that already finished answers already_absent —
            # so re-dialing costs a request and recovers a lost push. Attempts still count, so a
            # target that keeps saying "later" and never delivers eventually exhausts and shows up
            # in `stuck` like any other failure, which is the outcome an operator can act on.
            st["pending_since"] = st.get("pending_since") or now
            if st["attempts"] >= self._cfg.PRIVACY_MAX_ATTEMPTS:
                st["exhausted"] = True
                st["detail"] = (f"{detail} (accepted but never pushed an ack after "
                                f"{st['attempts']} attempts)")
                st["next_at"] = 0.0
            else:
                st["next_at"] = now + self._cfg.PRIVACY_PENDING_TIMEOUT_SECONDS
        else:
            exhausted = st["attempts"] >= self._cfg.PRIVACY_MAX_ATTEMPTS
            st["exhausted"] = exhausted
            # 0 = never due again: exhausted targets stop burning attempts but stay UNACKED, so the
            # DSAR cannot complete and the operator sees exactly which service is holding it up.
            st["next_at"] = 0.0 if exhausted else now + self._backoff(st["attempts"])

        state[key] = st
        self._emit(dsar, key, st)

    # ── async targets push their ack back here ──────────────────────────────────
    def record_pushed_ack(self, dsar, key: str, ack: dict | None) -> None:
        """A 202 target reporting in later. The ack map is still what completes the DSAR — this only
        attaches the evidence body so the record says WHAT was erased and what was kept."""
        state = _state_of(dsar)
        st = dict(state.get(key, {}))
        st.update(status=ACKED, ack=ack, next_at=0.0, last_at=self._clock.now())
        state[key] = st
        dsar.target_state = state

    # ── target resolution (config only — never from a request) ──────────────────
    def _url_for(self, service: str) -> str | None:
        base = self._cfg.privacy_targets.get(service)
        if not base:
            return None
        return base + self._cfg.erase_path_for(service)

    async def _token_for(self, service: str, tenant: str = "") -> str:
        """A third party gets ITS credential. Only our own services — the ones with no override —
        ever see the internal token.

        Resolution order is operator store, then the secrets port, then nothing. The port value is
        passed as the FALLBACK rather than checked afterwards, so "no override stored" and "no
        operator surface at all" resolve identically and an unmigrated deployment is unchanged.
        """
        key = self._cfg.secret_key_for(service)
        if key is None:
            return self._cfg.INTERNAL_SERVICE_TOKEN
        configured = ""
        if self._secrets is not None:
            try:
                configured = self._secrets.get(key) or ""
            except Exception:      # noqa: BLE001 — an unreachable vault is not a reason to raise
                configured = ""
        if self._credentials is None:
            return configured
        return await self._credentials.get(key, fallback=configured, tenant=tenant)

    # ── retry schedule ──────────────────────────────────────────────────────────
    def _is_due(self, st: dict) -> bool:
        if st.get("exhausted"):
            return False
        # PENDING has no special case here any more. It sets a next_at like everything else, so
        # "we are waiting for their push" and "we have waited long enough" are the same mechanism
        # rather than two — the short-circuit that used to live here is exactly what made a lost
        # push unrecoverable, because no elapsed time could ever change the answer.
        next_at = float(st.get("next_at") or 0.0)
        return next_at <= self._clock.now()

    def _backoff(self, attempts: int) -> float:
        """Full jitter: random in [0, base * 2^(n-1)], capped. Everything fails together in a
        partition; identical retry timing would turn recovery into a stampede."""
        ceiling_ms = self._cfg.PRIVACY_BACKOFF_MS * (2 ** min(attempts - 1, 10))
        pick = self._random() if self._random is not None else _jitter()
        return (ceiling_ms * pick) / 1000.0

    # ── reporting ───────────────────────────────────────────────────────────────
    def _summary(self, dsar, *, attempted: int) -> dict[str, Any]:
        state = _state_of(dsar)
        remaining = [k for k, acked in dsar.targets.items() if not acked]
        return {
            "id": dsar.id,
            "attempted": attempted,
            "acked": [k for k, acked in dsar.targets.items() if acked],
            "remaining": remaining,
            "stuck": [{"target": k, "attempts": state.get(k, {}).get("attempts", 0),
                       "detail": state.get(k, {}).get("detail", "")}
                      for k in remaining if state.get(k, {}).get("exhausted")],
            # Accepted, not yet pushed, and not yet exhausted. Reported SEPARATELY from `stuck`
            # because it is not a failure — the target may be legitimately mid-erasure. But it is
            # the state a lost push hides in, so it must be visible while it is still recoverable
            # rather than only once the retries have run out.
            "awaiting_push": [{"target": k, "since": state.get(k, {}).get("pending_since", 0.0)}
                              for k in remaining
                              if state.get(k, {}).get("status") == PENDING
                              and not state.get(k, {}).get("exhausted")],
            "complete": dsar.is_complete(),
        }

    def _emit_completed(self, dsar) -> None:
        if self._telemetry is None:
            return
        try:
            self._telemetry.event("privacy.dsar.completed",
                                  {"dsar": dsar.id, "targets": len(dsar.targets)})
        except Exception:
            pass

    def _emit(self, dsar, key: str, st: dict) -> None:
        if self._telemetry is None:
            return
        try:
            self._telemetry.event("privacy.erase.attempt", {
                "dsar": dsar.id, "target": key, "status": st.get("status"),
                "attempts": st.get("attempts"), "exhausted": bool(st.get("exhausted"))})
        except Exception:
            pass          # observability must never break an erasure


def _state_of(dsar) -> dict[str, dict]:
    return dict(getattr(dsar, "target_state", None) or {})


def _jitter() -> float:
    import secrets as _s
    return _s.randbelow(10_000) / 10_000.0
