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

from typing import Any

from app.core.identity_ref import claim_ref
from app.core.models import (ERROR, EXPIRED, PENDING, REFUSED, VERIFIED, Evidence, Verification,
                             new_id)
from app.ports import verifier as vp

DAY_SECONDS = 86400


class UnsupportedClaim(ValueError):
    """This platform does not verify this kind of claim.

    Refused loudly rather than answered by a default. A platform that has not configured a business
    verifier must not quietly return "verified" — or quietly return "refused" either, which would
    block every customer for a reason nobody can see.
    """


class VerificationService:
    def __init__(self, *, store, verifiers: dict, clock, pii_salt: str,
                 ttl_days: dict[str, int] | None = None, pending_ttl: int = 900,
                 registry=None, auditor=None, event_bus=None) -> None:
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
    async def resolve(self, verification_id: str, *, verified: bool, reference: str = "",
                      detail: str = "") -> Verification | None:
        """A provider's callback. IDEMPOTENT: a replay must not flip a settled verdict.

        Providers retry callbacks, and a retry that re-opened a decided verification would let a
        duplicate delivery overwrite a refusal with an approval — or the reverse. So only a record
        still PENDING can move, and anything else is returned unchanged.
        """
        record = await self._store.get(verification_id)
        if record is None:
            return None
        if record.verdict != PENDING:
            return record                       # already settled; a replay changes nothing

        now = self._clock.now()
        if record.pending_until and now >= record.pending_until:
            # The prompt lapsed before the answer arrived. Not approval, not refusal — nobody
            # answered, and a late yes cannot be trusted to have come from the right person.
            record.verdict = EXPIRED
            record.updated_at = now
            await self._store.put(record)
            return record

        outcome = vp.Outcome(status=vp.VERIFIED if verified else vp.REFUSED,
                             provider=record.evidence.get("provider", ""),
                             reference=reference, detail=detail)
        self._apply(record, outcome, now)
        record.provider_state = {}              # the correlation token has done its job
        await self._commit(record, outcome)
        return record

    async def sweep_pending(self, limit: int = 500) -> int:
        """Lapse prompts nobody answered. A safety net, not the authority — `status()` already
        refuses to trust a lapsed record, so a sweep that has not run cannot leave one trusted."""
        now = self._clock.now()
        lapsed = 0
        for record in await self._store.pending_before(now, limit=limit):
            record.verdict = EXPIRED
            record.updated_at = now
            await self._store.put(record)
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
        return {"claim_type": claim_type, "verdict": verdict, "trusted": trusted,
                "checked_at": record.evidence.get("checked_at", 0.0),
                "expires_at": record.expires_at,
                "reason": record.evidence.get("detail", "")}

    async def _commit(self, record: Verification, outcome: vp.Outcome) -> None:
        """Persist a settled record — write-ahead the critical grant audit BEFORE the put.

        identity.verified is declared critical and AUDIT_FAIL_CLOSED defaults True, so its
        auditor.record raises AuditUnavailable when the log is down. If we put() first (as the code
        did — verification-001) an unprovable VERIFIED verdict would already be stored: status()/
        latest() return VERIFIED and a downstream welcome-gift/account-open can act on it, while the
        caller only sees a 503 implying nothing happened. Recording the grant intent FIRST means a
        down audit log refuses the grant before it exists; the caller's retry then re-runs cleanly.
        A refusal/error/pending verdict is not trust-granting, so it is stored first and audited
        non-critically in _announce (refusing to refuse someone because the log is down helps nobody).
        """
        if record.verdict == VERIFIED and self._auditor is not None:
            await self._auditor.record(
                "identity.verified", actor=record.subject, tenant=record.tenant,
                resource=f"{record.claim_type}:{record.id}",
                metadata={"provider": outcome.provider, "reference": outcome.reference})
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
            except Exception:
                # A registry write failing must not un-verify a genuine identity — but it does mean
                # the anti-abuse key is missing, so the grant side refuses rather than assuming.
                pass

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
        if self._bus is not None:
            try:
                await self._bus.publish("verification.settled", {
                    "id": record.id, "tenant": record.tenant, "subject": record.subject,
                    "claim_type": record.claim_type, "verdict": record.verdict})
            except Exception:
                pass
