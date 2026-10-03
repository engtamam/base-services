"""VerificationService — turn a claim into a verdict somebody else can rely on.

Four rules shape this file, and each exists because getting it wrong is silent.

  1. **A verdict is only ever what a provider said.** There is no path where a timeout, a parse
     failure or a missing provider produces `verified`. Every unknown resolves to `error`.

  2. **Expiry is evaluated on READ, not by a sweeper.** A cancelled commercial registration must
     stop reading as verified the moment its TTL lapses, not whenever a cron next runs. A sweeper
     that has not fired yet is a window where a stale verdict is still trusted.

  3. **Async is a real state.** Nafath-style providers push a prompt to a phone and answer minutes
     later. `pending` is a first-class verdict, its callback is idempotent, and a prompt nobody
     answers LAPSES — lapsing is not approval.

  4. **The claim value lives only in memory.** It goes to the provider and into a hash. It is never
     written to the datastore, never logged, never returned.
"""
from __future__ import annotations

import logging
from typing import Any

from app.core.identity_ref import claim_ref
from app.core.models import (ERROR, EXPIRED, PENDING, REFUSED, VERIFIED, Evidence, Verification,
                             new_id)
from app.ports import verifier as vp

DAY_SECONDS = 86400
_log = logging.getLogger(__name__)


class UnsupportedClaim(ValueError):
    """This platform does not verify this kind of claim.

    Refused loudly rather than answered by a default. A platform that has not configured a business
    verifier must not quietly return "verified" — or quietly return "refused" either, which would
    block every customer for a reason nobody can see.
    """


class VerificationService:
    def __init__(self, *, store, verifiers: dict, clock, pii_salt: str,
                 ttl_days: dict[str, int] | None = None, pending_ttl: int = 900,
                 registry=None, auditor=None, event_bus=None, telemetry=None) -> None:
        self._store = store
        # The durable record of WHICH identities have ever been verified. Without it the
        # anti-abuse key does not exist and a welcome gift is farmable by re-registering.
        self._registry = registry
        self._verifiers = verifiers
        self._clock = clock
        self._salt = pii_salt
        self._ttl = ttl_days or {}
        self._pending_ttl = pending_ttl
        self._auditor = auditor
        self._bus = event_bus
        self._telemetry = telemetry

    # ── asking ───────────────────────────────────────────────────────────────
    async def verify(self, *, tenant: str, subject: str, claim_type: str, value: str,
                     extra: dict | None = None) -> Verification:
        """Check a claim now. Returns the Verification, whatever the verdict."""
        verifier = self._verifiers.get(claim_type)
        if verifier is None:
            raise UnsupportedClaim(
                f"this platform does not verify {claim_type!r} claims; configure a provider in "
                f"VERIFY_PROVIDERS or use manual review")

        now = self._clock.now()
        ref = claim_ref(self._salt, claim_type, value)

        record = Verification(id=new_id("ver_"), tenant=tenant, subject=subject,
                              claim_type=claim_type, claim_ref=ref, verdict=PENDING,
                              created_at=now, updated_at=now)

        outcome = await self._ask(verifier, claim_type, value, tenant, subject, extra or {})
        self._apply(record, outcome, now)
        await self._commit(record, outcome)
        return record

    async def _ask(self, verifier, claim_type: str, value: str, tenant: str, subject: str,
                   extra: dict) -> vp.Outcome:
        """Call the provider. The port forbids raising, but a provider adapter is ordinary code
        and a bug in it must not become a verified identity."""
        claim = vp.Claim(claim_type=claim_type, value=value, tenant=tenant, subject=subject,
                         extra=extra)
        try:
            return await verifier.verify(claim)
        except Exception as exc:
            return vp.Outcome(status=vp.ERROR, provider=getattr(verifier, "driver", "?"),
                              detail=f"verifier raised: {exc}")

    def _apply(self, record: Verification, outcome: vp.Outcome, now: float) -> None:
        """Translate a provider outcome into stored state. The only place a verdict is set."""
        record.evidence = Evidence(provider=outcome.provider, reference=outcome.reference,
                                   checked_at=now, detail=outcome.detail).to_dict()
        record.updated_at = now

        if outcome.status == vp.VERIFIED:
            record.verdict = VERIFIED
            record.expires_at = self._expiry_for(record.claim_type, now)
            record.pending_until = 0.0
        elif outcome.status == vp.REFUSED:
            record.verdict = REFUSED
            record.pending_until = 0.0
        elif outcome.status == vp.PENDING:
            record.verdict = PENDING
            # A prompt nobody answers must lapse rather than sit trusted-adjacent forever.
            record.pending_until = now + self._pending_ttl
            record.provider_state = dict(outcome.state or {})
        else:
            record.verdict = ERROR
            record.pending_until = 0.0

    def _expiry_for(self, claim_type: str, now: float) -> float:
        """When this verdict stops being trustworthy. 0 = never, for claims that cannot change."""
        days = self._ttl.get(claim_type, 0)
        return now + days * DAY_SECONDS if days > 0 else 0.0

    # ── the provider answering later ─────────────────────────────────────────
    class CorrelationMismatch(Exception):
        """The resolve callback did not present the provider correlation token the pending prompt
        stored (verification-007). The caller is refused rather than allowed to settle a prompt it
        cannot prove it owns."""

    async def resolve(self, verification_id: str, *, verified: bool, reference: str = "",
                      detail: str = "") -> Verification | None:
        """A provider's callback. IDEMPOTENT and CONCURRENCY-SAFE: a replay or a duplicate retried
        callback must not flip a settled verdict, double-settle, or double-audit.

        Only a still-PENDING record can move. The settle is an optimistic compare-and-set (cas_put)
        with refetch-on-conflict (verification-003): two retried callbacks can no longer both read
        PENDING and both write — the loser refetches and returns the now-settled record unchanged.
        The critical grant audit is written AHEAD of the cas commit (verification-004 pattern).
        """
        for _ in range(8):
            record = await self._store.get(verification_id)
            if record is None:
                return None
            if record.verdict != PENDING:
                return record                   # already settled; a replay changes nothing

            now = self._clock.now()
            if record.pending_until and now >= record.pending_until:
                # The prompt lapsed before the answer arrived. Not approval, not refusal — nobody
                # answered, and a late yes cannot be trusted to have come from the right person.
                record.verdict = EXPIRED
                record.updated_at = now
                if await self._store.cas_put(record):
                    return record
                continue                        # lost the race — refetch and re-evaluate

            # verification-007: prove the callback owns this pending prompt. The pending outcome stored
            # the provider's correlation reference in provider_state; a callback that cannot present it
            # (when one was captured) may not settle a verification whose id is returned to the user.
            expected = str((record.provider_state or {}).get("reference", ""))
            if expected and reference != expected:
                raise self.CorrelationMismatch(
                    "resolve reference does not match the pending prompt's provider correlation token")

            outcome = vp.Outcome(status=vp.VERIFIED if verified else vp.REFUSED,
                                 provider=record.evidence.get("provider", ""),
                                 reference=reference, detail=detail)
            self._apply(record, outcome, now)
            record.provider_state = {}          # the correlation token has done its job
            # Write-ahead the critical grant audit BEFORE the cas commit (verification-004): a down
            # audit log refuses the grant before it is stored, never after.
            await self._audit_grant(record, outcome)
            if await self._store.cas_put(record):
                await self._announce(record, outcome)
                return record
            # Lost the cas race: another writer settled it first. Loop; the next read returns settled.
        return await self._store.get(verification_id)

    async def sweep_pending(self, limit: int = 500) -> int:
        """Lapse prompts nobody answered. A safety net, not the authority — `status()` already
        refuses to trust a lapsed record, so a sweep that has not run cannot leave one trusted.

        Each lapse is a compare-and-set (verification-006): a provider callback or a reviewer can
        settle a record to VERIFIED/REFUSED between the find() and the write, and an unconditional
        put() would clobber that real decision with EXPIRED. cas_put + refetch skips anything no
        longer PENDING."""
        now = self._clock.now()
        lapsed = 0
        for stale in await self._store.pending_before(now, limit=limit):
            record = await self._store.get(stale.id)
            if record is None or record.verdict != PENDING:
                continue                        # settled between find() and now — leave the decision
            if not (record.pending_until and now >= record.pending_until):
                continue
            record.verdict = EXPIRED
            record.updated_at = now
            if await self._store.cas_put(record):   # skip on conflict — a concurrent settle wins
                lapsed += 1
        return lapsed

    # ── reading ──────────────────────────────────────────────────────────────
    async def status(self, tenant: str, subject: str, claim_type: str) -> dict[str, Any]:
        """Is this subject's claim currently good?

        `trusted` is computed HERE against the clock rather than read from a stored flag, so an
        expired verdict stops being trusted the instant it lapses.
        """
        now = self._clock.now()
        record = await self._store.latest(tenant, subject, claim_type)
        if record is None:
            return {"claim_type": claim_type, "verdict": None, "trusted": False,
                    "reason": "never checked"}

        trusted = record.is_trusted(now)
        verdict = record.verdict
        if verdict == VERIFIED and not trusted:
            # Stale, not refused. The caller needs to know to RE-ASK rather than to give up: a
            # lapsed registration may well still be valid, we simply have not looked recently.
            verdict = EXPIRED
        elif verdict == PENDING and record.pending_until and now >= record.pending_until:
            # verification-005: evaluate the lapse on READ, exactly as VERIFIED expiry is. A prompt
            # nobody answered must not report 'pending' forever when the sweep CronJob has not yet run
            # and no callback arrived — a caller polling /status would otherwise wait indefinitely.
            verdict = EXPIRED
        # User-safe reason code (verification-012): map the verdict to a small fixed set rather than
        # echoing record.evidence["detail"], which is the RAW provider refusal/error text (an internal
        # message, sometimes leaking provider internals). The raw detail stays in evidence for the
        # operator/review/audit surface — it is simply not handed to the end user.
        _SAFE_REASON = {VERIFIED: "verified", REFUSED: "refused", PENDING: "pending",
                        EXPIRED: "expired", ERROR: "unavailable"}
        return {"claim_type": claim_type, "verdict": verdict, "trusted": trusted,
                "checked_at": record.evidence.get("checked_at", 0.0),
                "expires_at": record.expires_at,
                "reason": _SAFE_REASON.get(verdict, "")}

    async def _audit_grant(self, record: Verification, outcome: vp.Outcome) -> None:
        """Write-ahead the critical identity.verified audit (verification-001/004).

        identity.verified is declared critical and AUDIT_FAIL_CLOSED defaults True, so this
        auditor.record raises AuditUnavailable when the log is down — and it MUST be called BEFORE the
        verdict is persisted. Persisting first would store an unprovable VERIFIED verdict (status()/
        latest() return VERIFIED, downstream acts on it) while the caller sees only a 503; recording
        the grant intent first means a down log refuses the grant before it exists.
        """
        if record.verdict == VERIFIED and self._auditor is not None:
            await self._auditor.record(
                "identity.verified", actor=record.subject, tenant=record.tenant,
                resource=f"{record.claim_type}:{record.id}",
                metadata={"provider": outcome.provider, "reference": outcome.reference})

    async def _commit(self, record: Verification, outcome: vp.Outcome) -> None:
        """The FRESH-record persist path (verify()): write-ahead the grant audit, create, announce.
        resolve()/sweep_pending()/erasure use cas_put directly for their concurrent-update paths."""
        await self._audit_grant(record, outcome)
        await self._store.put(record)
        await self._announce(record, outcome)

    async def _announce(self, record: Verification, outcome: vp.Outcome) -> None:
        """Remember the verified identity, emit the settled event, and audit non-grant verdicts.

        The critical VERIFIED audit is NOT here — it is written ahead of the put in _commit
        (verification-001) so a down audit log refuses the grant before it is stored. Refusals and
        errors are recorded here NON-critically: they are the signal fraud monitoring turns on, but a
        down audit log must not block a refusal — refusing to refuse someone helps nobody.
        """
        if record.verdict == VERIFIED and self._registry is not None:
            # Remember the IDENTITY, not the person. This row survives account deletion and is the
            # only thing standing between a welcome gift and someone collecting it repeatedly by
            # deleting the tenant and signing up again.
            try:
                await self._registry.remember(record.claim_ref, self._clock.now())
            except Exception as exc:
                # A registry write failing must not un-verify a genuine identity — but it does mean
                # the anti-abuse key is missing, so the grant side refuses rather than assuming.
                # verification-011: make that MISS observable — a silently-missing anti-abuse key on a
                # VERIFIED identity is exactly the gap this registry exists to close.
                if self._telemetry is not None:
                    try:
                        self._telemetry.event("verification.registry_remember_failed",
                                              {"tenant": record.tenant, "claim_type": record.claim_type,
                                               "error": type(exc).__name__})
                    except Exception:
                        pass
                _log.warning("verification registry.remember failed for a VERIFIED identity "
                             "(anti-abuse key missing): %s", type(exc).__name__)

        if self._auditor is not None:
            # A refusal and a provider error are recorded NON-critically: an identity check that
            # failed or was refused is exactly the signal fraud monitoring turns on (PCI-DSS 10.2 /
            # NIST AU-2), but — unlike the VERIFIED grant written ahead in _commit — a down audit log
            # must not block a refusal, since refusing to refuse someone helps nobody.
            if record.verdict == REFUSED:
                await self._auditor.record(
                    "identity.refused", actor=record.subject, tenant=record.tenant,
                    resource=f"{record.claim_type}:{record.id}",
                    metadata={"provider": outcome.provider, "reference": outcome.reference})
            elif record.verdict == ERROR:
                await self._auditor.record(
                    "identity.check_failed", actor=record.subject, tenant=record.tenant,
                    resource=f"{record.claim_type}:{record.id}",
                    metadata={"provider": outcome.provider,
                              "detail": str(record.evidence.get("detail", ""))})
            elif record.verdict == PENDING:
                # verification-015: an async check that awaits a phone prompt is an ATTEMPT that must be
                # witnessed — otherwise an initiated check that later lapses leaves no trace at all.
                # Non-critical (a down log must not block starting the check).
                await self._auditor.record(
                    "identity.check_pending", actor=record.subject, tenant=record.tenant,
                    resource=f"{record.claim_type}:{record.id}",
                    metadata={"provider": outcome.provider, "reference": outcome.reference})
        if self._bus is not None:
            try:
                await self._bus.publish("verification.settled", {
                    "id": record.id, "tenant": record.tenant, "subject": record.subject,
                    "claim_type": record.claim_type, "verdict": record.verdict})
            except Exception:
                pass
