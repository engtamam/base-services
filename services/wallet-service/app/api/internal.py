# =============================================================================
# wallet-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
#
# ALL money movement lives here. A customer must never be able to credit their
# own wallet, so the user tier (/api/v1/wallet) is read-only.
# =============================================================================
from __future__ import annotations


from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.api.v1.wallet import build_wallet
from app.backup_manifest import BACKUP
from app.config import Settings, get_settings
from app.core import backup as _backup
from app.core import funds as _funds
from app.core.erasure import WalletErasure
from app.core.money import MoneyError
from app.core.wallet_service import Conflict, WalletError
from app.manifest import build_manifest
from app.core.pii import subject_ref

router = APIRouter(prefix="/internal", tags=["internal"])

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


class CreditIn(BaseModel):
    tenant: str = Field(min_length=1, max_length=200)
    fund: str = Field(min_length=1, max_length=64)
    amount: int = Field(ge=0)                          # integer MINOR units; never a float
    source: str = Field(default="", max_length=200)    # idempotency key
    reason: str = Field(default="", max_length=200)
    expires_at: float | None = None                    # a grant: unused remainder is clawed back


class SpendIn(BaseModel):
    tenant: str = Field(min_length=1, max_length=200)
    net: int = Field(ge=0)                             # the PRE-VAT price, in minor units
    source: str = Field(default="", max_length=200)
    reason: str = Field(default="", max_length=200)
    # Optional fund allowlist: restrict this spend/hold to the named fund(s) so one purpose can never
    # drain another's balance. Omit (null) for the historical all-funds behaviour. An empty list or an
    # unknown fund name is refused (a money-path restriction is never guessed).
    funds: list[str] | None = Field(default=None)


class WithdrawIn(BaseModel):
    tenant: str = Field(min_length=1, max_length=200)
    amount: int = Field(ge=1)                          # MINOR units; a payout is never zero/negative
    destination: str = Field(default="", max_length=256)   # opaque payout target reference
    source: str = Field(default="", max_length=200)    # idempotency key
    reason: str = Field(default="", max_length=200)


class SettleWithdrawalIn(BaseModel):
    withdrawal_id: str = Field(min_length=1, max_length=100)
    paid: bool                                          # True = transfer settled; False = reverse it
    source: str = Field(default="", max_length=200)


class HoldIn(BaseModel):
    hold_id: str = Field(min_length=1, max_length=100)


class ConfirmIn(HoldIn):
    # Optional: confirm for a SMALLER real net than was reserved (the reserve was a conservative
    # ceiling). The difference is auto-released. Omit to confirm the full reserved amount. Must be
    # <= the reserved net; the core refuses a larger value (spending unreserved money).
    final_net: int | None = Field(default=None, ge=0)


class RefundIn(BaseModel):
    tenant: str = Field(min_length=1, max_length=200)
    seq: int = Field(ge=1)                             # ledger sequence of the spend to reverse
    source: str = Field(default="", max_length=200)
    reason: str = Field(default="", max_length=200)


def _handle(exc: Exception) -> HTTPException:
    """Money errors are 4xx and never leak internals; contention is a retryable 409."""
    if isinstance(exc, _funds.InsufficientFunds):
        return HTTPException(status_code=402, detail="INSUFFICIENT_FUNDS")
    if isinstance(exc, Conflict):
        return HTTPException(status_code=409, detail="WALLET_CONTENTION")
    if isinstance(exc, (WalletError, MoneyError)):
        return HTTPException(status_code=400, detail=str(exc))
    raise exc


@router.get("/wallet/{tenant}", dependencies=[Depends(require_internal_token)])
async def get_balance(tenant: str, request: Request) -> dict:
    return await build_wallet(request).balance(tenant)


@router.get("/wallet/{tenant}/ledger", dependencies=[Depends(require_internal_token)])
async def get_ledger(tenant: str, request: Request,
                     limit: int = Query(default=50, ge=1, le=500),
                     before_seq: int | None = Query(default=None, ge=1)) -> dict:
    """Internal-token twin of GET /wallet/admin/ledger — a tenant's individual ledger rows for the
    (cross-repo) dispute-defense report. Same bounded ledger() the self/admin routes read (the
    service clamps `limit` to LEDGER_LIST_LIMIT); read-only, money never moves. Audited: an internal
    caller reading a customer's money is exactly the access a review asks about."""
    await request.app.state.auditor.record(
        "wallet.inspected", actor="", tenant=tenant, resource="wallet",
        ip=request.client.host if request.client else "", metadata={"view": "ledger"})
    rows = await build_wallet(request).ledger(tenant, limit=limit, before_seq=before_seq)
    return {"tenant": tenant, "entries": rows, "count": len(rows),
            "next_before_seq": rows[-1]["seq"] if rows else None}


@router.get("/wallet/{tenant}/reconcile", dependencies=[Depends(require_internal_token)])
async def reconcile(tenant: str, request: Request) -> dict:
    """Prove the materialized balance equals sum(ledger). Two writes, one truth."""
    return await build_wallet(request).reconcile(tenant)


@router.post("/wallet/credit", dependencies=[Depends(require_internal_token)])
async def credit(body: CreditIn, request: Request) -> dict:
    """Add money. Call this only once the PSP has SETTLED — recording an intent is not money."""
    svc = build_wallet(request)
    # Validate the inputs BEFORE the write-ahead audit: a rejected request (unknown fund / negative
    # amount) moves no money, so it must not emit the critical wallet.credited row. Validation is
    # non-mutating, so this does not weaken the write-ahead guarantee for the actual mutation below.
    try:
        svc.validate_credit(fund=body.fund, amount=body.amount)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "wallet.credited", actor="", tenant=body.tenant, resource="wallet",
        ip=request.client.host if request.client else "")
    try:
        return await svc.credit(
            tenant=body.tenant, fund=body.fund, amount=body.amount, source=body.source,
            reason=body.reason, expires_at=body.expires_at)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc


@router.post("/wallet/spend", dependencies=[Depends(require_internal_token)])
async def spend(body: SpendIn, request: Request) -> dict:
    """Direct debit. Refuses rather than overdraw, and a refusal mutates nothing."""
    try:
        result = await build_wallet(request).spend(
            tenant=body.tenant, net=body.net, source=body.source, reason=body.reason,
            funds=tuple(body.funds) if body.funds is not None else None)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
    # Non-critical, so recorded after the debit succeeds. Internal seam: the caller holds the
    # service token, so there is no user principal — actor is empty, the source IP identifies the peer.
    await request.app.state.auditor.record(
        "wallet.spent", actor="", tenant=body.tenant, resource="wallet",
        ip=request.client.host if request.client else "", metadata={"source": body.source})
    return result


@router.post("/wallet/withdraw", dependencies=[Depends(require_internal_token)])
async def withdraw(body: WithdrawIn, request: Request) -> dict:
    """Pay money OUT to an external account. Internal, like credit/spend: the CALLER (a payout
    orchestrator) is the one that verified the account may withdraw (the KYC / wallet.payout gate);
    this debits only withdrawable funds and records a pending Withdrawal. Idempotent by `source`."""
    # Write-ahead: a critical action the log cannot take must not happen at all, so this runs BEFORE
    # the debit — money leaving the platform is exactly the action a review turns on.
    await request.app.state.auditor.record(
        "wallet.withdrawn", actor="", tenant=body.tenant, resource="wallet",
        ip=request.client.host if request.client else "",
        metadata={"amount": str(body.amount), "source": body.source})
    try:
        return await build_wallet(request).withdraw(
            tenant=body.tenant, amount=body.amount, destination=body.destination,
            source=body.source, reason=body.reason)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc


@router.post("/wallet/withdrawals/settle", dependencies=[Depends(require_internal_token)])
async def settle_withdrawal(body: SettleWithdrawalIn, request: Request) -> dict:
    """The payout sidecar reports the transfer's result. paid=False REVERSES the debit — a transfer
    that did not happen must never cost the customer money. Idempotent on the withdrawal's state."""
    try:
        result = await build_wallet(request).settle_withdrawal(
            withdrawal_id=body.withdrawal_id, paid=body.paid, source=body.source)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
    await request.app.state.auditor.record(
        "wallet.withdrawal_settled", actor="", tenant="", resource=f"withdrawal:{body.withdrawal_id}",
        ip=request.client.host if request.client else "",
        metadata={"paid": str(body.paid), "state": str(result.get("state", ""))})
    return result


@router.post("/wallet/reserve", dependencies=[Depends(require_internal_token)])
async def reserve(body: SpendIn, request: Request) -> dict:
    """Hold funds for an in-flight action. The hold EXPIRES; it cannot strand the money."""
    try:
        result = await build_wallet(request).reserve(
            tenant=body.tenant, net=body.net, source=body.source, reason=body.reason,
            funds=tuple(body.funds) if body.funds is not None else None)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
    await request.app.state.auditor.record(
        "wallet.reserved", actor="", tenant=body.tenant, resource="wallet",
        ip=request.client.host if request.client else "", metadata={"source": body.source})
    return result


@router.post("/wallet/confirm", dependencies=[Depends(require_internal_token)])
async def confirm(body: ConfirmIn, request: Request) -> dict:
    """The action succeeded: consume the hold. Replaying this must not charge twice.

    Pass `final_net` to charge the real (usually smaller) amount instead of the reserved ceiling —
    the difference is auto-released. Omit it for the full reserved amount."""
    try:
        result = await build_wallet(request).confirm(hold_id=body.hold_id, final_net=body.final_net)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
    await request.app.state.auditor.record(
        "wallet.confirmed", actor="",
        tenant=str(result.get("tenant", "")) if isinstance(result, dict) else "",
        resource=f"hold:{body.hold_id}", ip=request.client.host if request.client else "")
    return result


@router.post("/wallet/release", dependencies=[Depends(require_internal_token)])
async def release(body: HoldIn, request: Request) -> dict:
    """The action failed: free the hold. Nothing was debited, so nothing is refunded."""
    try:
        result = await build_wallet(request).release(hold_id=body.hold_id)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
    await request.app.state.auditor.record(
        "wallet.released", actor="",
        tenant=str(result.get("tenant", "")) if isinstance(result, dict) else "",
        resource=f"hold:{body.hold_id}", ip=request.client.host if request.client else "")
    return result


@router.post("/wallet/refund", dependencies=[Depends(require_internal_token)])
async def refund(body: RefundIn, request: Request) -> dict:
    """Reverse a spend INTO THE FUNDS IT CAME FROM (a refunded gift stays a gift)."""
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "wallet.refunded", actor="", tenant=body.tenant, resource="wallet",
        ip=request.client.host if request.client else "")
    try:
        return await build_wallet(request).refund(tenant=body.tenant, seq=body.seq,
                                                  source=body.source, reason=body.reason)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc


@router.post("/wallet/sweep-holds", dependencies=[Depends(require_internal_token)])
async def sweep_holds(request: Request) -> dict:
    """Release abandoned holds. Without this a dead client locks its credits forever."""
    result = await build_wallet(request).sweep_holds()
    # A scheduled sweep has no principal — the platform scheduler is the actor, recorded as "system".
    await request.app.state.auditor.record(
        "wallet.holds_swept", actor="system", resource="wallet",
        ip=request.client.host if request.client else "",
        metadata={k: str(v) for k, v in result.items()} if isinstance(result, dict) else None)
    return result


@router.post("/wallet/sweep-grants", dependencies=[Depends(require_internal_token)])
async def sweep_grants(request: Request) -> dict:
    """Claw back the unused remainder of expired grants. The customer keeps what they spent."""
    result = await build_wallet(request).sweep_grants()
    await request.app.state.auditor.record(
        "wallet.grants_swept", actor="system", resource="wallet",
        ip=request.client.host if request.client else "",
        metadata={k: str(v) for k, v in result.items()} if isinstance(result, dict) else None)
    return result


@router.post("/wallet/notice-grants", dependencies=[Depends(require_internal_token)])
async def notice_grants(request: Request) -> dict:
    """Emit the one-time pre-expiry notice for grants entering their fund's notice window. Scheduler-
    driven, alongside sweep-grants."""
    return await build_wallet(request).notice_grants()


class ReclaimIn(BaseModel):
    grant_id: str = Field(min_length=1, max_length=128)
    source: str = Field(default="", max_length=200)
    reason: str = Field(default="", max_length=500)


@router.post("/wallet/reclaim", dependencies=[Depends(require_internal_token)])
async def reclaim_grant(request: Request, body: ReclaimIn) -> dict:
    """Restore a DORMANT grant's preserved remainder to spendable — the primitive a future 'reclaim
    idle balance' flow calls. Recorded: moving money back into a customer's balance is a privileged act."""
    try:
        result = await build_wallet(request).reclaim(
            grant_id=body.grant_id, source=body.source, reason=body.reason)
    except Exception as exc:  # noqa: BLE001
        # Map GRANT_NOT_FOUND / GRANT_NOT_DORMANT / contention to a status, like withdraw/settle —
        # an unmapped raise would surface as a 500 instead of a clean 400/409.
        raise _handle(exc) from exc
    await request.app.state.auditor.record(
        "wallet.grant_reclaimed", actor="system", resource=f"grant:{body.grant_id}",
        ip=request.client.host if request.client else "",
        metadata={"amount": str(result.get("reclaimed", 0)), "fund": str(result.get("fund", ""))})
    return result


@router.get("/health", dependencies=[Depends(require_internal_token)])
async def internal_health(settings: Settings = Depends(get_settings)) -> dict[str, str]:
    return {"status": "ok", "service": settings.SERVICE_NAME}


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


@router.post("/privacy/erase", dependencies=[Depends(require_internal_token)])
async def erase_subject(request: Request, body: EraseIn) -> dict:
    """wallet holds no subject-linked data — every row is keyed by tenant.

    The endpoint still exists so a DSAR can PROVE it asked. "never contacted" and "holds nothing"
    are indistinguishable in an incomplete system and completely different to a regulator.
    """
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "subject.erased", actor="", tenant=body.tenant, resource=f"subject:{subject_ref(get_settings().PII_SALT, body.tenant, body.subject)}",
        ip=request.client.host if request.client else "")
    s = get_settings()
    eraser = WalletErasure(store=request.app.state.container.datastore, pii_salt=s.PII_SALT)
    return await eraser.erase(body.tenant, body.subject)


# ── Backup (backup-service orchestrates; this service produces sealed archives) ──────────────
# What wallet-service holds is declared in app/backup_manifest.py; how it is read lives in
# core/backup.py. backup-service never opens this database — it calls export/verify/import over the
# internal token and stores the sealed archive, never seeing the plaintext. This is money: the
# ledger and its per-tenant sequence counter travel together so a restore reconciles.
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
    # AUTHORITATIVE undo: also delete rows present now but absent from the archive
    # (backup-r2-01). Set only by the undo path; never combined with into_tenant.
    prune: bool = False


@router.post("/backup/export", dependencies=[Depends(require_internal_token)])
async def backup_export(request: Request, body: BackupExportIn,
                        settings: Settings = Depends(get_settings)) -> dict:
    """Produce a sealed, checksummed archive of the wallets, ledger, holds, and grants."""
    seal, _ = _sealer(settings)
    return await _backup.export(
        request.app.state.container.datastore, BACKUP,
        service=settings.SERVICE_NAME, version=settings.SERVICE_VERSION,
        taken_at=_now_iso(), seal=seal, tenant=body.tenant,
        scan_cap=settings.BACKUP_MAX_ROWS_PER_COLLECTION)


@router.post("/backup/verify", dependencies=[Depends(require_internal_token)])
async def backup_verify(request: Request, body: BackupVerifyIn,
                        settings: Settings = Depends(get_settings)) -> dict:
    """Prove an archive restores, by test-restoring it into a throwaway namespace and comparing.

    The declaration is passed so the ledger's composite key ("{tenant}:{seq:012d}") and the wallet's
    tenant key are reconstructed for the verify, exactly as a real restore would.
    """
    import secrets
    _, unseal = _sealer(settings)
    try:
        return await _backup.verify_restore(
            request.app.state.container.datastore, body.archive,
            unseal=unseal, token=secrets.token_hex(8), declaration=BACKUP)
    except _backup.BackupError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.post("/backup/import", dependencies=[Depends(require_internal_token)])
async def backup_import(request: Request, body: BackupImportIn,
                        settings: Settings = Depends(get_settings)) -> dict:
    """Restore an archive this service produced. Idempotent; leaves newer rows alone by default.

    The declaration is passed so the ledger and wallet restore under their real composite/tenant
    keys — a ledger entry restored under its led_ id would silently drop out of the seq-ordered
    history reconcile() walks.
    """
    _, unseal = _sealer(settings)
    try:
        return await _backup.import_archive(
            request.app.state.container.datastore, body.archive,
            unseal=unseal, overwrite=body.overwrite, into_tenant=body.into_tenant,
            declaration=BACKUP, prune=body.prune,
            scan_cap=settings.BACKUP_MAX_ROWS_PER_COLLECTION)
    except _backup.BackupError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
