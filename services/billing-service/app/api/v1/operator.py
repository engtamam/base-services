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
from starlette.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api.auth import Principal, require_user
from app.api.authz import require_capability
from app.api.ratelimit import rate_limit
from app.config import get_settings
from app.core import tax_profile
from app.core.pii import subject_ref
from app.core.runtime_config import RuntimeConfig, RuntimeConfigError
from app.core.secret_store import SecretError, SecretStore

router = APIRouter(prefix="/api/v1/billing/operator", tags=["operator"])


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
    """Drop this replica's resolved provider chains so the change is visible on the next send.

    The refresh window still exists and still matters: another replica's cache cannot be reached
    from here. This only makes the common case — an operator setting a key and immediately testing
    it — instant rather than up to a window late.
    """
    pool = getattr(request.app.state, "provider_pool", None)
    if pool is not None:
        pool.invalidate(tenant or None)
    # billing resolves payment credentials through the CredentialResolver, not a provider_pool — so a
    # just-set gateway key/driver is otherwise stale for up to the refresh window, and the operator's
    # "set the key then activate/test it" flow sees the OLD (missing) value. Invalidate it too.
    creds = getattr(request.app.state, "credentials", None)
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


# The keys whose value decides whether a tax invoice can be backed. A write to ANY of them is
# evaluated against the resulting WHOLE profile, so the mode and the calculator/rate/seller number
# behind it can never drift apart — the split that let a tenant be flipped to tax_invoice with no
# seller registration was the whole defect. See app/core/tax_profile.py.
_TAX_PROFILE_KEYS = frozenset({
    "BILLING_DOCUMENT_MODE", "TAX_CALCULATOR_DRIVER", "VAT_RATE", "INVOICE_SELLER_VAT",
    "EINVOICER_DRIVER"})


async def _guard_tax_profile(request: Request, *, tenant: str, key: str,
                             new_value: object, cleared: bool) -> None:
    """Refuse a config change that would leave a tax-issuing tenant with an INCOMPLETE tax profile,
    naming the missing key — the same judgement _mark_paid and the document routes make, applied at
    the write so the panel refuses it up front rather than the next paid invoice rendering a void
    document. A no-op for a non-tax key or a receipt-mode tenant. Raises TaxProfileIncomplete."""
    if key not in _TAX_PROFILE_KEYS:
        return
    rc = _runtime_config(request)
    s = get_settings()

    async def eff(k: str) -> object:
        default = getattr(s, k, None)
        if k == key:
            return default if cleared else new_value   # the value this change WOULD result in
        try:
            return await rc.get(tenant=tenant, key=k, default=default)
        except Exception:  # noqa: BLE001 — a completeness read must not 500 the config write
            return default

    # The mode is per tenant; the seller number is the platform's. Evaluated TOGETHER (effective
    # scope resolves tenant override then the platform value), so completeness reflects what a paid
    # invoice for this tenant would actually see.
    tax_profile.check(
        str(await eff("BILLING_DOCUMENT_MODE")),
        tax_driver=str(await eff("TAX_CALCULATOR_DRIVER")),
        vat_rate=await eff("VAT_RATE"),
        seller_vat=str(await eff("INVOICE_SELLER_VAT") or ""),
        einvoicer_driver=str(await eff("EINVOICER_DRIVER")))


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


# Subscription statuses that RENEW by charging the saved token — the ones a gateway switch breaks,
# because the new gateway cannot charge a token it never issued.
_RENEWABLE_STATUSES = ("active", "past_due", "trialing")


async def _gateway_activation_error(request: Request, *, tenant: str, driver: str) -> str | None:
    """None if the gateway can be activated, else a person-facing reason.

    A gateway is activatable iff its own adapter BUILDS with the tenant's resolved credentials — every
    adapter raises GatewayUnavailable when its required fields are missing, so this reuses the exact
    check that guards a real charge and can never drift from what the gateway actually needs. `null`
    (off) needs no credentials."""
    from app.container import _PAYMENT
    from app.core.credentials import Overlay
    from app.ports.payment_gateway import GatewayUnavailable

    name = (driver or "").strip()
    if name not in _PAYMENT:
        return f"unknown payment gateway {name!r}"
    resolver = getattr(request.app.state, "credentials", None)
    resolved = await resolver.resolve(tenant) if resolver is not None else {}
    try:
        _PAYMENT[name](Overlay(get_settings(), resolved))     # trial build — never charges
    except GatewayUnavailable as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001 — any builder error is a not-activatable answer, not a 500
        return type(exc).__name__
    return None


async def _current_driver(request: Request, tenant: str) -> str:
    """The tenant's currently-active gateway (runtime override, else the boot driver)."""
    s = get_settings()
    rc = getattr(request.app.state, "runtime_config", None)
    if rc is None:
        return s.PAYMENT_GATEWAY_DRIVER
    try:
        return str(await rc.get(tenant=tenant, key="PAYMENT_GATEWAY_DRIVER",
                                default=s.PAYMENT_GATEWAY_DRIVER)) or s.PAYMENT_GATEWAY_DRIVER
    except Exception:  # noqa: BLE001 — a preview read must never 500; fall to the boot driver
        return s.PAYMENT_GATEWAY_DRIVER


async def _tokenized_renewable_count(request: Request, tenant: str, *, cap: int = 500) -> int:
    """How many of this tenant's renewable subscriptions hold a saved payment token — the subscribers
    a gateway switch would break (their token belongs to the old gateway; the new one cannot charge
    it). One indexed, bounded read (billable_for), never a scan. Capped: the count is a warning for a
    human, not an exact ledger, so ">= cap" is a fine answer at the extreme."""
    subs = getattr(request.app.state, "container", None)
    if subs is None:
        return 0
    from app.core.stores import SubscriptionStore
    rows = await SubscriptionStore(subs.datastore).billable_for(
        tenant, _RENEWABLE_STATUSES, limit=cap)
    return sum(1 for r in rows if r.get("payment_token"))


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
    # A tax invoice may not be enabled from — or left standing on — an incomplete profile. Raises
    # TaxProfileIncomplete, which has its own handler so the refusal reaches the operator BY NAME.
    await _guard_tax_profile(request, tenant=body.tenant, key=body.key,
                             new_value=body.value, cleared=False)
    # A payment gateway may only be ACTIVATED if its own credentials are already set — refuse the
    # switch outright otherwise (a real validation, not a UI hint), so the money path is never pointed
    # at an unconfigured provider.
    if body.key == "PAYMENT_GATEWAY_DRIVER":
        err = await _gateway_activation_error(request, tenant=body.tenant, driver=str(body.value or ""))
        if err is not None:
            return _refuse(f"cannot activate {str(body.value or '')!r}: {err}",
                           code="GATEWAY_NOT_CONFIGURED")
    try:
        o = await _runtime_config(request).set(
            tenant=body.tenant, key=body.key, value=body.value, actor_ref=_actor(principal))
    except RuntimeConfigError as exc:
        return _refuse(str(exc))
    _invalidate(request, body.tenant)      # EMAIL_CHAIN/SMS_CHAIN reorder the providers
    # The tenant is NOT echoed. The caller supplied it, so returning it adds nothing, and a
    # response field named "tenant" is indistinguishable — to a reader and to the tenancy gate —
    # from a verified claim. The one wall between every company on this platform is that the
    # tenant is believed everywhere and checked in exactly one place; nothing here should look
    # like that check.
    return {"key": o.key, "value": o.value}


@router.get("/payment/activation-preview", dependencies=[Depends(_CONFIG), Depends(rate_limit)])
async def payment_activation_preview(request: Request, driver: str, tenant: str = "",
                                     principal: Principal = Depends(require_user)) -> dict:
    """What activating `driver` would do — so a switch is a deliberate, informed choice, not a silent
    breakage found later via failed renewals.

    Answers two things the admin screen shows BEFORE the confirm:
      * activatable + reason — can this gateway even go live (its own credentials present)? Same check
        the actual switch enforces.
      * affected_subscribers — how many of the tenant's renewable subscriptions hold a saved payment
        token that belongs to the CURRENT gateway and would therefore break on the switch (their
        holders must re-enter payment via the self-service update route). 0 when there is no real
        switch (same driver) — the common pre-launch case. null platform-wide (tenant omitted): the
        count is per-tenant and a fleet-wide scan is not a request-path operation.
    """
    err = await _gateway_activation_error(request, tenant=tenant, driver=driver)
    current = await _current_driver(request, tenant)
    switching = (driver or "").strip() != (current or "").strip()
    if not tenant:
        affected: int | None = None                 # not computed platform-wide (no fleet scan)
    elif not switching:
        affected = 0                                 # no switch → no token is orphaned
    else:
        affected = await _tokenized_renewable_count(request, tenant)
    return {"driver": driver, "current_driver": current, "switching": switching,
            "activatable": err is None, "reason": err or "",
            "affected_subscribers": affected}


@router.delete("/config/{key}", dependencies=[Depends(_CONFIG), Depends(rate_limit)])
async def clear_config(request: Request, key: str, tenant: str = "",
                       principal: Principal = Depends(require_user)) -> dict:
    """Drop an override so the configured default applies again."""
    await request.app.state.auditor.record(
        "operator.config_changed", actor=_actor(principal), tenant=tenant,
        resource=f"config:{key}", ip=request.client.host if request.client else "",
        metadata={"key": key, "cleared": True})
    # Clearing an override falls back to the configured default — which, for a seller number, may be
    # empty. Refuse the clear (by name, via the TaxProfileIncomplete handler) if it would leave a
    # tax-issuing tenant unable to back a tax invoice.
    await _guard_tax_profile(request, tenant=tenant, key=key, new_value=None, cleared=True)
    cleared = await _runtime_config(request).clear(tenant=tenant, key=key)
    _invalidate(request, tenant)
    return {"cleared": cleared}
