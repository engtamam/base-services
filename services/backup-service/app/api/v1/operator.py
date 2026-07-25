"""The operator surface — credentials and policy, changeable after launch.

WRITE-ONLY IS ENFORCED HERE, and this file is where the guarantee lives. `SecretStore.get()`
returns plaintext because an adapter cannot authenticate with a masked string; no route in this
module returns it, and `safety:no-secret-reads` fails the build if one ever does. That split is
deliberate: the store is a mechanism, the API boundary is the control.

`platform.secrets` IS ITS OWN CAPABILITY. Setting a payment key and reading a report are not the
same privilege, and a panel that conflates them hands every report-reader the ability to redirect
money.

EVERY MUTATION IS AUDITED WRITE-AHEAD AND CRITICAL. A credential change that cannot be recorded
must not happen: an unprovable one is indistinguishable from an attacker's, and the previous value
is already gone by the time anybody looks.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.auth import Principal, require_user
from app.api.authz import require_capability
from app.api.ratelimit import rate_limit
from app.config import get_settings
from app.core.pii import subject_ref
from app.core.runtime_config import RuntimeConfig, RuntimeConfigError
from app.core.secret_store import SecretError, SecretStore
from app.core.restore import RestoreError, RestoreOrchestrator
from app.core.restores import RestoreStore
from app.core.runs import RunStore
from app.core.targets import TargetStore

router = APIRouter(prefix="/api/v1/operator", tags=["operator"])

_SECRETS = require_capability("platform.secrets")
_CONFIG = require_capability("platform.config")
# Restoring overwrites live data — its own capability, never folded into config or secrets. An
# operator who may reorder providers must not, by that fact, be able to overwrite the platform.
_RESTORE = require_capability("platform.restore")


def _actor(principal: Principal) -> str:
    """A pseudonym. An audit row naming the operator is still personal data, and this one lands in
    an append-only chain."""
    s = get_settings()
    if not s.PII_SALT:
        return ""
    return subject_ref(s.PII_SALT, principal.tenant or "", principal.subject)


def _secret_store(request: Request) -> SecretStore:
    s = get_settings()
    if not s.SECRETS_KEK:
        # A clear refusal rather than a 500. Without a KEK there is nothing to seal with, and
        # storing the credential anyway is the one outcome that must never happen.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "SECRETS_UNAVAILABLE")
    return SecretStore(request.app.state.container.datastore, s.SECRETS_KEK,
                       rotation_window_s=s.SECRETS_ROTATION_WINDOW_SECONDS)


def _runtime_config(request: Request) -> RuntimeConfig:
    """The instance wired at boot, so the service's RUNTIME_POLICY applies.

    Building a fresh one here would silently carry no policy, and a RuntimeConfig with no policy
    refuses everything — the panel would report every knob as unsettable while the service believed
    it had wired them.
    """
    rc = getattr(request.app.state, "runtime_config", None)
    if rc is not None:
        return rc
    from app.config import Settings
    from app.runtime_policy import RUNTIME_POLICY

    return RuntimeConfig(request.app.state.container.datastore, Settings, policy=RUNTIME_POLICY)


class SecretIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    value: str = Field(..., min_length=1, max_length=8192)
    # Omitted means platform-wide. A tenant may hold its own credential where the provider is
    # per-customer; most are not.
    tenant: str = Field(default="", max_length=128)


class ConfigIn(BaseModel):
    key: str = Field(..., min_length=1, max_length=128)
    value: str | int | float | bool
    tenant: str = Field(default="", max_length=128)


# ── credentials ───────────────────────────────────────────────────────────────
@router.get("/secrets", dependencies=[Depends(_SECRETS), Depends(rate_limit)])
async def list_secrets(request: Request, tenant: str = "",
                       principal: Principal = Depends(require_user)) -> dict:
    """Which credentials are set, and a four-character hint for each.

    There is no value in this response and no route that would return one. The hint exists so an
    operator can tell which key is loaded before replacing it — replacing the wrong one is the
    mistake this prevents.
    """
    store = _secret_store(request)
    infos = await store.list_info(tenant=tenant)
    return {"secrets": [{"name": i.name, "hint": i.hint, "set_at": i.set_at,
                         "rotating": i.rotating} for i in infos]}


@router.put("/secrets", dependencies=[Depends(_SECRETS), Depends(rate_limit)])
async def set_secret(request: Request, body: SecretIn,
                     principal: Principal = Depends(require_user)) -> dict:
    """Set or rotate a credential. The previous one stays valid for a bounded window.

    Audited BEFORE the write: a change the log cannot take must not happen, and here the old value
    is overwritten by the action itself, so an unrecorded rotation is unreconstructable.
    """
    await request.app.state.auditor.record(
        "operator.secret_set", actor=_actor(principal), tenant=body.tenant or "",
        resource=f"secret:{body.name}",          # the NAME, never the value or the hint
        ip=request.client.host if request.client else "")
    try:
        info = await _secret_store(request).set(
            tenant=body.tenant, name=body.name, value=body.value, actor_ref=_actor(principal))
    except SecretError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"name": info.name, "hint": info.hint, "rotating": info.rotating}


@router.delete("/secrets/{name}", dependencies=[Depends(_SECRETS), Depends(rate_limit)])
async def clear_secret(request: Request, name: str, tenant: str = "",
                       principal: Principal = Depends(require_user)) -> dict:
    """Remove a credential so the configured environment value applies again."""
    await request.app.state.auditor.record(
        "operator.secret_cleared", actor=_actor(principal), tenant=tenant,
        resource=f"secret:{name}", ip=request.client.host if request.client else "")
    return {"cleared": await _secret_store(request).clear(tenant=tenant, name=name)}


# ── policy ────────────────────────────────────────────────────────────────────
@router.get("/config", dependencies=[Depends(_CONFIG), Depends(rate_limit)])
async def effective_config(request: Request, tenant: str = "",
                           principal: Principal = Depends(require_user)) -> dict:
    """Every knob an operator may change, with its current effective value.

    Built from the settable set, so a credential, a DSN or a frozen identifier cannot appear here
    even by accident — the filter is in one place rather than repeated per field.
    """
    rc = _runtime_config(request)
    return {"config": await rc.effective(tenant=tenant, settings=get_settings())}


@router.put("/config", dependencies=[Depends(_CONFIG), Depends(rate_limit)])
async def set_config(request: Request, body: ConfigIn,
                     principal: Principal = Depends(require_user)) -> dict:
    """Override one policy value. Refused if the boot guard would refuse it."""
    await request.app.state.auditor.record(
        "operator.config_changed", actor=_actor(principal), tenant=body.tenant or "",
        resource=f"config:{body.key}", ip=request.client.host if request.client else "",
        metadata={"key": body.key})     # the KEY is not a secret; settable knobs never are
    try:
        o = await _runtime_config(request).set(
            tenant=body.tenant, key=body.key, value=body.value, actor_ref=_actor(principal))
    except RuntimeConfigError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    # The tenant is NOT echoed. The caller supplied it, so returning it adds nothing, and a
    # response field named "tenant" is indistinguishable — to a reader and to the tenancy gate —
    # from a verified claim. The one wall between every company on this platform is that the
    # tenant is believed everywhere and checked in exactly one place; nothing here should look
    # like that check.
    return {"key": o.key, "value": o.value}


@router.delete("/config/{key}", dependencies=[Depends(_CONFIG), Depends(rate_limit)])
async def clear_config(request: Request, key: str, tenant: str = "",
                       principal: Principal = Depends(require_user)) -> dict:
    """Drop an override so the configured default applies again."""
    await request.app.state.auditor.record(
        "operator.config_changed", actor=_actor(principal), tenant=tenant,
        resource=f"config:{key}", ip=request.client.host if request.client else "",
        metadata={"key": key, "cleared": True})
    return {"cleared": await _runtime_config(request).clear(tenant=tenant, key=key)}


# ── restore — the one dangerous action ─────────────────────────────────────────
def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _restore_orchestrator(request: Request) -> RestoreOrchestrator:
    from app.core.token_resolver import make_token_resolver
    s = get_settings()
    c = request.app.state.container
    return RestoreOrchestrator(
        targets=TargetStore(c.datastore), object_store=c.object_store,
        runs=RunStore(c.datastore), restores=RestoreStore(c.datastore),
        resolve_token=make_token_resolver(getattr(request.app.state, "secret_store", None)),
        iso=_now_iso,
        cfg={"egress_allowed_hosts": s.egress_allowed_hosts,
             "target_timeout_seconds": s.BACKUP_TARGET_TIMEOUT_SECONDS})


class RestorePlanIn(BaseModel):
    run_id: str = Field(..., min_length=1, max_length=128)
    tenant: str = Field(default="", max_length=128)
    services: list[str] = Field(default_factory=list, max_length=64)
    require_verified: bool = True


class RestoreIn(BaseModel):
    run_id: str = Field(..., min_length=1, max_length=128)
    # Echo-to-confirm: the caller must repeat the run id they intend to restore. A restore is not
    # something a mis-click or a replayed request should ever trigger; the intent must be explicit.
    confirm: str = Field(..., min_length=1, max_length=128)
    tenant: str = Field(default="", max_length=128)
    # Restore-as-clone: stand this backup up under a DIFFERENT tenant (migration, sandbox, repro).
    into_tenant: str = Field(default="", max_length=128)
    services: list[str] = Field(default_factory=list, max_length=64)
    overwrite: bool = False
    require_verified: bool = True
    safety_snapshot: bool = True


class UndoIn(BaseModel):
    services: list[str] = Field(default_factory=list, max_length=64)


@router.post("/restore/plan", dependencies=[Depends(_RESTORE), Depends(rate_limit)])
async def restore_plan(request: Request, body: RestorePlanIn,
                       principal: Principal = Depends(require_user)) -> dict:
    """Preview what a restore WOULD do — which targets are eligible, which are blocked and why, and
    how many rows each holds. Writes nothing; this is the screen an operator confirms against."""
    return await _restore_orchestrator(request).plan(
        body.run_id, tenant=body.tenant, services=body.services,
        require_verified=body.require_verified)


@router.post("/restore", dependencies=[Depends(_RESTORE), Depends(rate_limit)])
async def run_restore(request: Request, body: RestoreIn,
                      principal: Principal = Depends(require_user)) -> dict:
    """Restore a backup run. Overwrites live data, so: capability-gated, echo-confirmed, audited
    write-ahead, snapshot-before-overwrite, and never aborting the whole restore on one target.

    Audited BEFORE it runs and as CRITICAL: a bulk overwrite of live data that cannot be recorded
    must not happen — 'who restored the platform, from which backup, into which tenant' is exactly
    what an incident review will need, and it is unreconstructable after the fact.
    """
    if body.confirm != body.run_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "CONFIRM_MISMATCH")
    await request.app.state.auditor.record(
        "backup.restored", actor=_actor(principal), tenant=body.tenant or "",
        resource=f"backup_run:{body.run_id}", ip=request.client.host if request.client else "",
        metadata={"into_tenant": body.into_tenant, "overwrite": body.overwrite,
                  "require_verified": body.require_verified,
                  "services": ",".join(body.services)})
    try:
        return await _restore_orchestrator(request).restore(
            body.run_id, actor=_actor(principal), tenant=body.tenant,
            into_tenant=body.into_tenant, services=body.services, overwrite=body.overwrite,
            require_verified=body.require_verified, safety_snapshot=body.safety_snapshot)
    except RestoreError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.post("/restore/{restore_id}/undo", dependencies=[Depends(_RESTORE), Depends(rate_limit)])
async def undo_restore(request: Request, restore_id: str,
                       body: UndoIn = UndoIn(),  # noqa: B008 — an empty body is the common case
                       principal: Principal = Depends(require_user)) -> dict:
    """Roll a restore back by re-applying each target's pre-restore snapshot. Audited, critical."""
    await request.app.state.auditor.record(
        "backup.restore_undone", actor=_actor(principal),
        resource=f"restore:{restore_id}", ip=request.client.host if request.client else "",
        metadata={"services": ",".join(body.services)})
    try:
        return await _restore_orchestrator(request).undo(
            restore_id, actor=_actor(principal), services=body.services)
    except RestoreError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.get("/restores", dependencies=[Depends(_RESTORE), Depends(rate_limit)])
async def list_restores(request: Request,
                        principal: Principal = Depends(require_user)) -> dict:
    """The restore history — every restore and undo, newest first."""
    limit = get_settings().BACKUP_RUN_HISTORY
    return {"restores": await RestoreStore(request.app.state.container.datastore).recent(limit)}


@router.get("/backup/health", dependencies=[Depends(_RESTORE), Depends(rate_limit)])
async def backup_health(request: Request, principal: Principal = Depends(require_user)) -> dict:
    """The panel's backup-health view: is every service protected by a recent verified backup, and
    which are not. Read-only; the same truth the monitor scrapes and the alerter pushes."""
    from app.core import health as _health
    s = get_settings()
    c = request.app.state.container
    targets = [t.public() for t in await TargetStore(c.datastore).all(s.BACKUP_TARGET_LIMIT)]
    runs = await RunStore(c.datastore).recent(s.BACKUP_RUN_HISTORY)
    stale_after = s.BACKUP_STALE_AFTER_SECONDS
    rc = getattr(request.app.state, "runtime_config", None)
    if rc is not None:
        try:
            stale_after = int(await rc.get(tenant="", key="BACKUP_STALE_AFTER_SECONDS",
                                           default=stale_after))
        except (TypeError, ValueError):
            pass
    return _health.evaluate(targets=targets, runs=runs, now_iso=_now_iso(),
                            stale_after_seconds=stale_after)
