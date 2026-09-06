# =============================================================================
# api-gateway — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations


from pydantic import BaseModel

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from app.config import Settings, get_settings
from app.core.erasure import GatewayErasure
from app.manifest import build_manifest

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


@router.get("/health", dependencies=[Depends(require_internal_token)])
async def internal_health(settings: Settings = Depends(get_settings)) -> dict[str, str]:
    return {"status": "ok", "service": settings.SERVICE_NAME}


@router.get("/routes", dependencies=[Depends(require_internal_token)])
async def internal_routes(request: Request) -> dict:
    table = request.app.state.container.route_table
    return {"routes": [r.summary() for r in table.all()]}


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


@router.get("/assistant-catalog", dependencies=[Depends(require_internal_token)])
async def assistant_catalog(request: Request, settings: Settings = Depends(get_settings)) -> dict:
    """The UNFILTERED action catalog for a knowledge-base fetch (assistant-service's CatalogKnowledge
    Source points KB_CATALOG_URL here).

    Distinct from /api/v1/app/catalog and /api/v1/admin/catalog on purpose: those are USER-tier and
    filter to the caller's own capabilities (right for a browser). A KB ingest instead wants EVERY
    action, each with its `capability` preserved, and lets the assistant filter per-asking-user later
    at answer time. So this is INTERNAL-tier (service token, no user auth) and fetches with the
    all-caps sentinel — the SAME already-hardened build_catalog (SSRF-safe fetch, id/method/path/cap
    validation), merging each service's user_actions AND admin_actions into one services[] document so
    a single URL covers both. Reused, never reimplemented, so the validation cannot drift from the
    user-facing catalogs."""
    import httpx
    from app.core.admin_catalog import ServiceTarget, build_catalog, parse_targets
    from app.core.internal_client import assertion_headers_for_url

    targets = parse_targets(settings.ADMIN_SERVICES)
    token = settings.INTERNAL_SERVICE_TOKEN
    timeout = settings.UPSTREAM_TIMEOUT_SECONDS

    async def fetch(t: ServiceTarget) -> dict | None:
        # Internal token stays server-side; the URL is the CONFIGURED base_url — no declaration can
        # redirect it (the same anti-SSRF posture as the user-facing catalogs).
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(f"{t.base_url}/internal/manifest",
                                 headers={"X-Internal-Token": token,
                                          **assertion_headers_for_url(t.base_url)})
        return r.json() if r.status_code == 200 else None

    all_caps = frozenset({"*"})     # unfiltered: the KB holds every action; the caller filters later
    users = await build_catalog(targets, all_caps, fetch, field="user_actions")
    admins = await build_catalog(targets, all_caps, fetch, field="admin_actions")

    # Merge the two views: one row per service, its user + admin actions concatenated. Order is
    # preserved (user actions first) and services with neither are simply absent.
    merged: dict[str, list] = {}
    order: list[str] = []
    for view in (users, admins):
        for svc in view["services"]:
            name = svc["service"]
            if name not in merged:
                merged[name] = []
                order.append(name)
            merged[name].extend(svc["actions"])
    services = [{"service": name, "actions": merged[name]} for name in order]
    # Unreachable is the UNION across both fetches (a service down for one fetch is down for both);
    # reported, never hidden, so a KB missing a service's actions is diagnosable.
    unreachable = sorted(set(users["unreachable"]) | set(admins["unreachable"]))
    return {"services": services, "unreachable": unreachable}


class EraseIn(BaseModel):
    tenant: str
    subject: str


@router.post("/privacy/erase", dependencies=[Depends(require_internal_token)])
async def erase_subject(request: Request, body: EraseIn) -> dict:
    """Answer the erasure contract so a DSAR can prove it asked this service."""
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "subject.erased", actor="", tenant=body.tenant, resource=f"subject:{body.subject}",
        ip=request.client.host if request.client else "")
    c = request.app.state.container
    s = get_settings()
    eraser = GatewayErasure(store=c.datastore, pii_salt=s.PII_SALT)
    return await eraser.erase(body.tenant, body.subject)
