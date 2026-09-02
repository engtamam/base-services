# =============================================================================
# compliance-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations


from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.v1.compliance import (build_compliance, build_consent, build_consent_sweeper,
                                   build_dsar, build_retention, build_sweeper)
from app.backup_manifest import BACKUP
from app.config import Settings, get_settings
from app.core import backup as _backup
from app.core.erasure import ComplianceErasure
from app.manifest import build_manifest

router = APIRouter(prefix="/internal", tags=["internal"])


class AckIn(BaseModel):
    service: str = Field(min_length=1, max_length=128)
    category: str = Field(min_length=1, max_length=128)
    # The ack BODY from an async (202) target: what it erased, what it kept and why. Optional so
    # the existing contract still works, but without it the DSAR records THAT a target finished
    # and not WHAT it did — and "what was kept, on what basis" is the half a regulator asks about.
    ack: dict | None = None

_HEADER = "X-Internal-Token"
_ASSERTION_HEADER = "X-Internal-Assertion"


async def require_internal_token(
    request: Request,
    x_internal_token: str | None = Header(default=None, alias=_HEADER),
    x_internal_assertion: str | None = Header(default=None, alias=_ASSERTION_HEADER),
    settings: Settings = Depends(get_settings),
) -> None:
    """Authenticate the internal caller, then authorise it for THIS route. The name is unchanged so
    every existing `Depends(require_internal_token)` keeps working, but it now does two things:

      1. IDENTITY — a config-resolved (cached) driver verifies the caller. `shared` (default) is
         the timing-safe flat-token compare (today's behaviour, unnamed peer); `static`/`jwks` verify a
         per-caller RS256 assertion. Any failure is one generic 401 — never which check failed.
      2. POLICY — the per-route allow-list decides whether that caller may reach `request.url.path`. A
         route with no policy admits any authenticated caller (default-open), so identity alone never
         breaks a path; a named allow-list yields 403 for a caller not on it.

    The verifier + policy are resolved from config (cached), not the container, so this seam is
    identical in every service."""
    from app.core.internal_caller_factory import resolve_internal_caller, resolve_internal_policy
    from app.ports.internal_caller import InternalCallerError

    audience = settings.INTERNAL_CALLER_AUDIENCE or settings.SERVICE_NAME
    try:
        identity = resolve_internal_caller(settings).verify(
            token=x_internal_token or "", assertion=x_internal_assertion or "", audience=audience)
    except InternalCallerError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "UNAUTHORIZED")
    if not resolve_internal_policy(settings).permits(request.url.path, identity.caller):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "FORBIDDEN")


@router.get("/health", dependencies=[Depends(require_internal_token)])
async def internal_health(settings: Settings = Depends(get_settings)) -> dict[str, str]:
    return {"status": "ok", "service": settings.SERVICE_NAME}


class InternalTargetIn(BaseModel):
    tenant: str = Field(default="", max_length=128)
    service: str = Field(min_length=1, max_length=128)
    category: str = Field(min_length=1, max_length=128)
    description: str = ""


@router.post("/compliance/data-targets", dependencies=[Depends(require_internal_token)])
async def internal_register_target(request: Request, body: InternalTargetIn) -> dict:
    """Register a tenant's data-map target from a product's own provisioning saga — the internal-tier
    twin of POST /api/v1/compliance/data-targets (tenant in the body).

    WHY IT EXISTS: DsarService.dispatch refuses with NO_DATA_TARGETS_REGISTERED on an empty map, so
    NO DSAR can complete for a real shop — and the user-tier route needs compliance.manage, which a
    shop owner must never hold (it also grants DELETE /data-targets, NARROWING the erasure fan-out — a
    worse PDPL posture than the honest refusal). So the map is wired HERE, at the same trusted moment
    the signup seeds roles and the catalogue. Idempotency is the store's (a repeated target is fine)."""
    try:
        t = await build_compliance(request).register_target(
            tenant=body.tenant, service=body.service, category=body.category,
            description=body.description)
    except ValueError as e:
        # 422: well-formed, authorised, but the NAME is not a configured target — the message lists
        # the real set so a typo or an undeployed service is obvious.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))
    await request.app.state.auditor.record(
        "data_target.registered", actor="", tenant=body.tenant,
        resource=f"target:{t.id}", ip=request.client.host if request.client else "",
        metadata={"service": body.service, "category": body.category})
    return t.to_dict()


@router.get("/compliance/dsar/{dsar_id}/verification-code",
            dependencies=[Depends(require_internal_token)])
async def internal_dsar_verification_code(request: Request, dsar_id: str) -> dict:
    """Hand the DSAR verification code to the PRODUCT that owns delivery.

    The code is minted at DSAR creation to prove the subject controls their channel — but compliance
    holds no channel for them, so it reached no one and the verify step could never be completed. A
    product with the internal token fetches it here and delivers it under its OWN consent record, then
    completes POST /dsar/{id}/verify. Internal-tier only (never a user route), and it returns nothing
    once the DSAR is already verified — a spent code is not re-handed out."""
    from app.core.stores import DsarStore
    d = await DsarStore(request.app.state.container.datastore).get(dsar_id)
    if d is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    return {"dsar_id": d.id, "tenant": d.tenant, "verified": d.verified,
            "code": "" if d.verified else d.verification_token,
            "expires_at": d.verification_expires}


@router.get("/compliance/consent-check", dependencies=[Depends(require_internal_token)])
async def consent_check(request: Request, tenant: str, subject: str, purpose: str) -> dict:
    """HOT PATH — any service asks before processing personal data. Default DENY, cached."""
    return await build_consent(request).check(tenant=tenant, subject=subject, purpose=purpose)


@router.get("/compliance/hold-check", dependencies=[Depends(require_internal_token)])
async def hold_check(request: Request, tenant: str, subject: str) -> dict:
    """HOT PATH — a service asks before PURGING. Fail closed, cached by generation.

    Retention is decentralised: every service enforces its own period, and retention.py is
    explicit that a hold beats retention and a held row must be reported as RETAINED rather than
    silently skipped. Until now that was impossible to honour from outside — holds lived only on
    the capability-gated v1 API, so a consuming service had to either hand a nightly cron job an
    admin token or purge without checking. Both are bad, and one of them deletes evidence.

    Guarded by the internal token like consent-check, precisely so a sweeper does not need
    `compliance.manage`. A retention job should be able to ask "may I delete this" without also
    being able to place and release holds.

    Returns hold_ids, not just a bool, so the caller can NAME what stopped it in its own receipt.
    """
    return await build_compliance(request).hold_check(tenant=tenant, subject=subject)


@router.get("/compliance/held-refs", dependencies=[Depends(require_internal_token)])
async def held_refs(request: Request, tenant: str) -> dict:
    """Every held subject for a tenant, as PSEUDONYMS. One call per sweep.

    For the callers hold-check cannot serve. A service that purges by SCANNING — audit-service
    walks partitions by timestamp — would otherwise need one HTTP call per row. And a service that
    pseudonymises cannot use hold-check at all: its rows carry subject_ref, an HMAC with no
    inverse, so it has no subject to ask with, and giving it the salt to re-derive one would
    defeat the pseudonymisation.

    Both sides compute subject_ref from the same frozen product PII_SALT, so the caller intersects
    these refs against the ones it already holds. NO RAW IDENTIFIER CROSSES THE WIRE in either
    direction — which makes this the better shape on privacy grounds, not merely the cheaper one.

    ⚠ `complete: false` MEANS DO NOT PURGE. It says the tenant has more active holds than
    HELD_REFS_MAX and this list is partial. Deleting on a partial list destroys evidence somebody
    is legally required to keep, so a caller that proceeds anyway has done something worse than
    skipping its run.
    """
    s = get_settings()
    return await build_compliance(request).held_refs(tenant=tenant, limit=s.HELD_REFS_MAX)


@router.post("/compliance/dsar/{dsar_id}/target-ack", dependencies=[Depends(require_internal_token)])
async def target_ack(request: Request, dsar_id: str, body: AckIn) -> dict:
    """A service confirms it erased/exported its slice. Completion is derived from these."""
    res = await build_dsar(request).ack_target(dsar_id, body.service, body.category,
                                               ack=body.ack)
    if not res["ok"]:
        # A REFUSAL MUST NEVER WEAR A SUCCESS CODE. If this returned 200 with {"ok": false},
        # raise_for_status() would pass, the caller would ack the job, the target would never
        # flip, and the DSAR would hang for ever while every log line said OK — a false "she was
        # erased", which is the worst lie this service could tell. It has always raised; this
        # only sharpens WHICH refusal it is.
        #
        #   NOT_FOUND      -> 404  the DSAR does not exist
        #   UNKNOWN_TARGET -> 409  the DSAR exists, but this service/category is not one of its
        #                          targets. That is a CONFLICT with the job's state, not a
        #                          malformed request — 400 told the caller to fix its syntax when
        #                          the syntax was fine.
        s = get_settings()
        code = (s.COMPLIANCE_ACK_NOTFOUND_STATUS if res["reason"] == "NOT_FOUND"
                else s.COMPLIANCE_ACK_REJECT_STATUS)
        raise HTTPException(code, res["reason"])
    # The ack is another service ASSERTING it destroyed personal data, and DSAR completion is
    # derived from these — so this is the state transition that closes a DSAR, and it was the one
    # with no record behind it. "Who told us this target had erased its copy" was unanswerable.
    #
    # The actor is the SERVICE, not a person: this is an internal call and there is no human on
    # it. Naming the caller is the whole value of the entry, so it is recorded as `service:<name>`
    # rather than left empty — the empty-actor rule exists because a nameless entry proves
    # nothing, and that reasoning applies to machines too.
    #
    # Recorded only on the success path. An ack we REFUSED (unknown target, no such DSAR) did not
    # change anything, and logging it as though a target had acked would be the false completion
    # this endpoint's own comment exists to prevent.
    await request.app.state.auditor.record(
        "dsar.target_acked", actor=f"service:{body.service}", tenant=res.get("tenant", ""),
        resource=f"dsar:{dsar_id}", ip=request.client.host if request.client else "",
        metadata={"service": body.service, "category": body.category, "ack": body.ack})
    return res


@router.post("/compliance/dsar/run", dependencies=[Depends(require_internal_token)])
async def run_dsar_sweep(request: Request) -> dict:
    """Drive every open DSAR's unacked targets. Called by the erasure CronJob.

    This is the worker that makes an erasure an EFFECT rather than a record. Safe to call at any
    time and from any replica: acked targets are skipped and backoff suppresses duplicate dials, so
    an overlapping run costs a no-op. The response names anything STUCK — a target that exhausted
    its attempts and is holding a DSAR open — because a jammed erasure nobody can see is how an
    SLA breach becomes a regulatory one.
    """
    return await build_sweeper(request).run()


@router.post("/compliance/consent/migrate-purpose-keys",
             dependencies=[Depends(require_internal_token)])
async def migrate_purpose_keys(request: Request) -> dict:
    """One-shot: rewrite pre-normalisation consent rows under their normalised key.

    Deliberately an operator action rather than something that runs at boot. It edits the records
    that decide what the platform may lawfully do, and a migration that happens automatically on
    every deploy is one nobody is watching when it goes wrong.

    Idempotent — a second run finds nothing left to convert. The response reports CONFLICTS, where
    both spellings existed: those are resolved deny-beats-allow rather than merged, and naming
    them is the point, because a resolution nobody chose should at least be one somebody can see.
    """
    from app.core.stores import ConsentStore
    s = get_settings()
    result = await ConsentStore(request.app.state.container.datastore).migrate_purpose_keys(
        limit=s.CONSENT_SWEEP_MAX_PER_RUN)
    await request.app.state.auditor.record(
        "consent.purpose_keys_migrated", actor="system:operator", tenant="", resource="consent",
        ip=request.client.host if request.client else "",
        metadata={"migrated": result["migrated"], "conflicts": len(result["conflicts"])})
    return result


@router.post("/compliance/consent/sweep-lapses", dependencies=[Depends(require_internal_token)])
async def run_consent_sweep(request: Request) -> dict:
    """Announce guardian consents crossing the age of majority. Called by a CronJob.

    Until this existed, majority handling was one line in capacity.py — expires_at is set to the
    day the subject can speak for themself — and nothing watched that date. On their eighteenth
    birthday, check() simply started answering "expired": no notice to the controller, no prompt
    for the young adult's OWN consent. Processing stopped and from outside it looked like a bug.

    A lapse is not an error that surfaces itself; it is a date arriving, and a date arriving is
    only visible to something that looks.
    """
    await request.app.state.auditor.record(
        "consent.lapse_swept", actor="system:cron", tenant="", resource="consent",
        ip=request.client.host if request.client else "")
    return await build_consent_sweeper(request).run()


@router.post("/compliance/retention/run", dependencies=[Depends(require_internal_token)])
async def run_retention(request: Request) -> dict:
    """Purge records past their declared retention period. Called by the retention CronJob.

    Enforces the periods for the records THIS service owns; every other service enforces its own
    and declares it in its RoPA row. A legal hold beats retention exactly as it beats erasure, and
    a collection with no configured period is never purged — an undecided retention reads as KEEP.
    """
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "retention.purged", actor="", tenant="", resource="compliance_records",
        ip=request.client.host if request.client else "")
    return await build_retention(request).run()


@router.get("/manifest", dependencies=[Depends(require_internal_token)])
async def internal_manifest(
    request: Request, settings: Settings = Depends(get_settings)
) -> dict:
    container = getattr(request.app.state, "container", None)
    ports = container.describe() if container is not None else []
    manifest = build_manifest(settings, ports=ports)
    guard = getattr(request.app.state, "cwe_guard", None)
    if guard is not None:
        manifest["security"] = {"cwe_guard": guard.report()}
    return manifest

class EraseIn(BaseModel):
    tenant: str
    subject: str
    # The KIND of identifier `subject` is (pseudonym, phone_hash, internal_id). Optional; "" = today's
    # behaviour (match the verbatim value). When set and this service indexes by a DIFFERENT kind, the
    # request is refused explicitly rather than matched against nothing — see the handler.
    subject_kind: str = Field(default="", max_length=64)


@router.post("/privacy/erase", dependencies=[Depends(require_internal_token)])
async def erase_subject(request: Request, body: EraseIn) -> dict:
    """Erase a data subject. Called by compliance-service during a DSAR fan-out.

    Returns the ack: what was erased, and what was RETAINED with its legal basis. Idempotent —
    a DSAR retries, so erasing an already-erased subject must not fail.
    """
    s = get_settings()
    # EXPLICIT REFUSAL of an identifier kind we do not index — BEFORE the write-ahead audit, because a
    # refusal is not an erasure and must not be logged as one. A target that indexes by internal_id and
    # is handed a phone_hash would otherwise match zero rows and ack a clean "erased": a false completion
    # indistinguishable from a real one. We say so instead; the dispatcher classifies the flag as FAILED,
    # so the DSAR stays open and visible. Default (no configured kinds) accepts any kind, as before.
    indexed = s.privacy_indexed_subject_kinds
    if body.subject_kind and indexed and body.subject_kind not in indexed:
        return {"service": s.SERVICE_NAME, "erased": [], "retained": [], "already_absent": False,
                "unrecognized_subject_kind": True, "subject_kind": body.subject_kind,
                "indexed_kinds": indexed}
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "subject.erased", actor="", tenant=body.tenant, resource=f"subject:{body.subject}",
        ip=request.client.host if request.client else "")
    c = request.app.state.container
    eraser = ComplianceErasure(store=c.datastore, pii_salt=s.PII_SALT)
    return await eraser.erase(body.tenant, body.subject)


# ── Backup (backup-service orchestrates; this service produces sealed archives) ──────────────
# What compliance-service holds is declared in app/backup_manifest.py; how it is read lives in
# core/backup.py. These are legal records — backup-service never opens this database, it calls
# export/verify/import over the internal token and stores the sealed archive, never the plaintext.
def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sealer(settings: Settings):
    """The backup (seal, unseal) pair, or a 503 when no backup key is configured.

    Refused rather than defaulted: sealing with an empty key would write a plaintext-equivalent
    archive while every check reported it encrypted — the one outcome a backup must never have.
    """
    if not settings.BACKUP_KEK:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "BACKUP_KEK not configured — set this SHARED key (the SAME value backup-service holds) "
            "on this service, or its archives cannot be opened")
    return _backup.make_sealer(settings.BACKUP_KEK)


class BackupExportIn(BaseModel):
    tenant: str = ""


class BackupVerifyIn(BaseModel):
    archive: dict


class BackupImportIn(BaseModel):
    archive: dict
    overwrite: bool = False
    into_tenant: str = ""


@router.post("/backup/export", dependencies=[Depends(require_internal_token)])
async def backup_export(request: Request, body: BackupExportIn,
                        settings: Settings = Depends(get_settings)) -> dict:
    """Produce a sealed, checksummed archive of the consent, DSAR, RoPA, breach, and data-map records."""
    seal, _ = _sealer(settings)
    return await _backup.export(
        request.app.state.container.datastore, BACKUP,
        service=settings.SERVICE_NAME, version=settings.SERVICE_VERSION,
        taken_at=_now_iso(), seal=seal, tenant=body.tenant,
        scan_cap=settings.BACKUP_MAX_ROWS_PER_COLLECTION)


@router.post("/backup/verify", dependencies=[Depends(require_internal_token)])
async def backup_verify(request: Request, body: BackupVerifyIn,
                        settings: Settings = Depends(get_settings)) -> dict:
    """Prove an archive restores, by test-restoring it into a throwaway namespace and comparing."""
    import secrets
    _, unseal = _sealer(settings)
    try:
        return await _backup.verify_restore(
            request.app.state.container.datastore, body.archive,
            unseal=unseal, token=secrets.token_hex(8), declaration=BACKUP)
    except _backup.BackupError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


async def _rebuild_proof_seq(datastore, scan_cap: int) -> int:
    """Rebuild the consent-proof sequence counter from the proofs — it is DERIVED, not archived.

    Each proof's id begins with its zero-padded per-subject sequence ("<seq>::<uuid>"), so the next
    number for a (tenant, subject) is max(seq) over that subject's proofs. Idempotent and self-
    healing; without it the next appended proof would restart at 1 and collide with a restored proof.
    """
    highest: dict[str, int] = {}
    for row in await datastore.scan_all("consent_proof", scan_cap):
        pid, tenant, subject = row.get("id", ""), row.get("tenant", ""), row.get("subject", "")
        head = pid.split("::", 1)[0]
        if not head.isdigit():
            continue
        k = f"{tenant}::{subject}"
        highest[k] = max(highest.get(k, 0), int(head))
    for seq_key, n in highest.items():
        await datastore.put("consent_proof_seq", seq_key, {"n": n})
    return len(highest)


@router.post("/backup/import", dependencies=[Depends(require_internal_token)])
async def backup_import(request: Request, body: BackupImportIn,
                        settings: Settings = Depends(get_settings)) -> dict:
    """Restore an archive this service produced. Idempotent; leaves newer rows alone by default.

    After the proofs are restored, the per-subject proof-sequence counter is rebuilt from them — it
    is derived, not archived, so a restore that skipped it would let the next proof re-use a number.
    """
    datastore = request.app.state.container.datastore
    _, unseal = _sealer(settings)
    try:
        result = await _backup.import_archive(
            datastore, body.archive,
            unseal=unseal, overwrite=body.overwrite, into_tenant=body.into_tenant,
            declaration=BACKUP)
    except _backup.BackupError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    result["proof_seq_rebuilt"] = await _rebuild_proof_seq(
        datastore, settings.BACKUP_MAX_ROWS_PER_COLLECTION)
    return result
