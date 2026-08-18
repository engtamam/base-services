"""The operator surface — credentials and policy, changeable after launch.

WRITE-ONLY IS ENFORCED HERE, and this file is where the guarantee lives. `SecretStore.get()`
returns plaintext because an adapter cannot authenticate with a masked string; no route in this
module returns it, and `safety:no-secret-reads` fails the build if one ever does. That split is
deliberate: the store is a mechanism, the API boundary is the control.

`platform.secrets` IS ITS OWN CAPABILITY. Setting the model provider's key and reading a report are
not the same privilege, and a panel that conflates them hands every report-reader the ability to
point the assistant at an endpoint of their choosing.

EVERY MUTATION IS AUDITED WRITE-AHEAD AND CRITICAL. A credential change that cannot be recorded
must not happen: an unprovable one is indistinguishable from an attacker's, and the previous value
is already gone by the time anybody looks.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from starlette.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api.auth import Principal, require_user
from app.api.authz import require_capability
from app.api.ratelimit import rate_limit
from app.config import get_settings
from app.core.pii import subject_ref
from app.core.runtime_config import RuntimeConfig, RuntimeConfigError
from app.core.secret_store import SecretError, SecretStore

router = APIRouter(prefix="/api/v1/operator", tags=["operator"])


def _refuse(reason: str, *, status_code: int = status.HTTP_400_BAD_REQUEST,
            code: str = "BAD_REQUEST") -> JSONResponse:
    """A person-facing refusal for an OPERATOR route, with its reason intact.

    The global error handler strips an HTTPException's detail to a bare {"error": "BAD_REQUEST"}
    (CWE-209) — right for an untrusted client, wrong here: the operator is a privileged caller and the
    reason is a domain validation message ("unknown key", "value out of range") they must SEE to act
    on. So a refusal is RETURNED with its reason rather than raised into the stripper. The reason is
    domain text the route chose to surface, never an exception's internals or a stack trace.
    """
    return JSONResponse({"error": code, "reason": reason}, status_code=status_code)


_SECRETS = require_capability("platform.secrets")
_CONFIG = require_capability("platform.config")


def _actor(principal: Principal) -> str:
    """A pseudonym. An audit row naming the operator is still personal data, and this one lands in
    an append-only chain."""
    s = get_settings()
    if not s.PII_SALT:
        return ""
    return subject_ref(s.PII_SALT, principal.tenant or "", principal.subject)


def _invalidate(request: Request, tenant: str) -> None:
    """Drop this replica's resolved model credential so the change is visible on the next question.

    The refresh window still exists and still matters: another replica's cache cannot be reached
    from here. This only makes the common case — an operator setting a key and immediately asking a
    question to check it — instant rather than up to a window late.
    """
    creds = getattr(request.app.state, "model_credentials", None)
    if creds is not None:
        creds.invalidate(tenant or None)


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
        return _refuse(str(exc))
    _invalidate(request, body.tenant)
    return {"name": info.name, "hint": info.hint, "rotating": info.rotating}


@router.delete("/secrets/{name}", dependencies=[Depends(_SECRETS), Depends(rate_limit)])
async def clear_secret(request: Request, name: str, tenant: str = "",
                       principal: Principal = Depends(require_user)) -> dict:
    """Remove a credential so the configured environment value applies again."""
    await request.app.state.auditor.record(
        "operator.secret_cleared", actor=_actor(principal), tenant=tenant,
        resource=f"secret:{name}", ip=request.client.host if request.client else "")
    cleared = await _secret_store(request).clear(tenant=tenant, name=name)
    _invalidate(request, tenant)
    return {"cleared": cleared}


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
        return _refuse(str(exc))
    _invalidate(request, body.tenant)      # a knob may change how the model call is made
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
    cleared = await _runtime_config(request).clear(tenant=tenant, key=key)
    _invalidate(request, tenant)
    return {"cleared": cleared}
