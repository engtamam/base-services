# =============================================================================
# connector-service — connections, provider specs, sync, DLQ, health
# =============================================================================
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.auth import Principal
from app.api.authz import require_any_capability
from app.config import get_settings
from app.core import seal as _seal
from app.core import ssrf as _ssrf
from app.core.mapping import MappingError, validate as validate_mapping
from app.core.models import Connection, ProviderSpec, REVOKED, new_id
from app.core.spec_resolver import resolve_spec
from app.core.stores import (ConnectionStore, DlqStore, RunStore, SeenStore, SpecStore)
from app.core.sync_engine import SyncEngine
from app.core.token_manager import TokenManager
from app.ports.connector_provider import CredentialInvalid, ProviderUnavailable

router = APIRouter(prefix="/api/v1/connectors", tags=["connectors"])

_ADMIN = require_any_capability("platform.connectors", "connectors.manage")
_READ = require_any_capability("platform.connectors", "connectors.manage", "connectors.read")


def _cfg() -> dict[str, Any]:
    s = get_settings()
    return {"batch_size": s.SYNC_BATCH_SIZE, "max_attempts": s.SYNC_MAX_ATTEMPTS,
            "retry_base_seconds": s.SYNC_RETRY_BASE_SECONDS,
            "retry_max_seconds": s.SYNC_RETRY_MAX_SECONDS, "retry_jitter": s.SYNC_RETRY_JITTER,
            "circuit_threshold": s.CIRCUIT_FAILURE_THRESHOLD,
            "circuit_reset_seconds": s.CIRCUIT_RESET_SECONDS}


def build_tokens(request: Request) -> TokenManager:
    c = request.app.state.container
    s = get_settings()
    return TokenManager(connections=ConnectionStore(c.datastore), provider=c.provider,
                        clock=c.clock, seal_key=s.seal_key, cache=c.cache,
                        cache_ttl=s.CONNECTION_CACHE_TTL_SECONDS,
                        refresh_skew=s.TOKEN_REFRESH_SKEW_SECONDS)


def build_sync(request: Request) -> SyncEngine:
    c = request.app.state.container
    return SyncEngine(connections=ConnectionStore(c.datastore), runs=RunStore(c.datastore),
                      dlq=DlqStore(c.datastore), seen=SeenStore(c.datastore),
                      tokens=build_tokens(request), provider=c.provider, clock=c.clock,
                      config=_cfg(), event_bus=c.event_bus)


def _tenant(p: Principal) -> str:
    if "platform.connectors" in p.caps:
        return p.tenant or "global"
    return p.tenant or "__none__"


async def _owned(request: Request, connection_id: str, p: Principal) -> Connection:
    conn = await ConnectionStore(request.app.state.container.datastore).get(connection_id)
    if conn is None or ("platform.connectors" not in p.caps and conn.tenant != (p.tenant or "__none__")):
        raise HTTPException(404, "NOT_FOUND")
    return conn


async def _spec_for(request: Request, conn: Connection) -> dict[str, Any]:
    """The provider spec for this connection.

    The credential resolution itself lives in `core/spec_resolver`, not here: the resolved spec
    carries a plaintext client secret, and a dict holding a live credential inside a route module
    is one `return` away from being a response.
    """
    return await resolve_spec(
        request.app.state.container.datastore,
        getattr(request.app.state, "secret_store", None),
        get_settings(), conn)


class SpecIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    token_url: str = ""
    base_url: str = ""
    revoke_url: str = ""
    auth_style: str = Field(default="body", pattern="^(body|basic)$")
    client_id: str = ""
    client_secret: str = ""
    endpoints: dict[str, Any] = Field(default_factory=dict)
    field_map: dict[str, Any] = Field(default_factory=dict)


class ConnectIn(BaseModel):
    provider: str = Field(min_length=1, max_length=64)
    spec_id: str = ""
    code: str = ""                 # OAuth2 authorization code
    redirect_uri: str = ""
    instance_url: str = ""         # self-hosted (SSRF-validated)


class SyncIn(BaseModel):
    connection_id: str
    resource: str = Field(min_length=1, max_length=128)
    direction: str = Field(default="pull", pattern="^(pull|push)$")
    records: list[dict[str, Any]] = Field(default_factory=list)


# ── provider specs (make a provider config, not code) ──────────────────────────
@router.post("/specs", status_code=201)
async def create_spec(request: Request, body: SpecIn, principal: Principal = Depends(_ADMIN)) -> dict:
    s = get_settings()
    try:
        if body.field_map:
            validate_mapping(body.field_map)
    except MappingError as e:
        raise HTTPException(422, str(e))
    # SSRF: every server-fetched URL on a spec is validated, not just base_url. token_url and
    # revoke_url are fetched by the OAuth adapter (urlopen), so an unvalidated one lets an admin point
    # the server at an internal/metadata address (e.g. http://169.254.169.254/...). All three share
    # the same allowlist/private-IP policy.
    for _label, _url in (("base_url", body.base_url), ("token_url", body.token_url),
                         ("revoke_url", body.revoke_url)):
        if not _url:
            continue
        try:
            _ssrf.validate(_url, require_https=s.INSTANCE_REQUIRE_HTTPS,
                           allow_private=s.INSTANCE_ALLOW_PRIVATE_IPS,
                           allowed_hosts=s.instance_allowed_hosts)
        except _ssrf.SsrfBlocked as e:
            raise HTTPException(400, f"URL_REJECTED ({_label}): {e}")
    spec = ProviderSpec(id=new_id("spec_"), tenant=_tenant(principal), name=body.name,
                        token_url=body.token_url, base_url=body.base_url, revoke_url=body.revoke_url,
                        auth_style=body.auth_style, client_id=body.client_id,
                        client_secret_sealed=_seal.seal(body.client_secret, s.seal_key),
                        endpoints=body.endpoints, field_map=body.field_map,
                        created=request.app.state.container.clock.now())
    saved = (await SpecStore(request.app.state.container.datastore).save(spec)).public()
    await request.app.state.auditor.record(
        "connector.spec_registered", actor=principal.subject, tenant=_tenant(principal),
        resource=f"spec:{spec.id}", ip=request.client.host if request.client else "")
    return saved


@router.get("/specs", dependencies=[Depends(_READ)])
async def list_specs(request: Request, principal: Principal = Depends(_READ)) -> dict:
    rows = await SpecStore(request.app.state.container.datastore).list(
        _tenant(principal), get_settings().CONNECTOR_LIST_LIMIT)
    return {"specs": [ProviderSpec.from_dict(r).public() for r in rows]}


# ── connections ────────────────────────────────────────────────────────────────
@router.post("/connect", status_code=201)
async def connect(request: Request, body: ConnectIn, principal: Principal = Depends(_ADMIN)) -> dict:
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "connector.connected", actor=principal.subject, tenant=principal.tenant, resource="connection",
        ip=request.client.host if request.client else "")
    s = get_settings()
    c = request.app.state.container
    if body.instance_url:
        try:
            _ssrf.validate(body.instance_url, require_https=s.INSTANCE_REQUIRE_HTTPS,
                           allow_private=s.INSTANCE_ALLOW_PRIVATE_IPS,
                           allowed_hosts=s.instance_allowed_hosts)
        except _ssrf.SsrfBlocked as e:
            raise HTTPException(400, f"URL_REJECTED: {e}")

    # IDOR: spec_id is caller-supplied. A connection may only use a spec in the CALLER'S tenant —
    # otherwise a shop could connect using another shop's OAuth application (its client_id/secret).
    caller_tenant = _tenant(principal)
    if body.spec_id:
        spec_row = await SpecStore(c.datastore).get(body.spec_id)
        if spec_row is None or spec_row.tenant != caller_tenant:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "SPEC_NOT_FOUND")
    conn = Connection(id=new_id("cn_"), tenant=caller_tenant, provider=body.provider,
                      spec_id=body.spec_id, instance_url=body.instance_url, created=c.clock.now())
    await ConnectionStore(c.datastore).save(conn)
    spec = await _spec_for(request, conn)
    try:
        tokens = c.provider.exchange(code=body.code, redirect_uri=body.redirect_uri, spec=spec)
    except (CredentialInvalid, ProviderUnavailable) as e:
        raise HTTPException(400, f"CONNECT_FAILED: {type(e).__name__}")
    tm = build_tokens(request)
    await tm.store_tokens(conn, tokens, invalidate=True)
    return conn.public()          # credentials are never returned


@router.get("", dependencies=[Depends(_READ)])
async def list_connections(request: Request, principal: Principal = Depends(_READ)) -> dict:
    rows = await ConnectionStore(request.app.state.container.datastore).list(
        _tenant(principal), get_settings().CONNECTOR_LIST_LIMIT)
    return {"connections": [Connection.from_dict(r).public() for r in rows]}


@router.get("/{connection_id}/health", dependencies=[Depends(_READ)])
async def health(request: Request, connection_id: str, principal: Principal = Depends(_READ)) -> dict:
    await _owned(request, connection_id, principal)
    return await build_sync(request).health(connection_id)


@router.post("/{connection_id}/disconnect")
async def disconnect(request: Request, connection_id: str, principal: Principal = Depends(_ADMIN)) -> dict:
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "connector.disconnected", actor=principal.subject, tenant=principal.tenant, resource=f"connection:{connection_id}",
        ip=request.client.host if request.client else "")
    conn = await _owned(request, connection_id, principal)
    c = request.app.state.container
    spec = await _spec_for(request, conn)
    revoked = False
    try:
        token = _seal.unseal(conn.access_sealed, get_settings().seal_key) or ""
        if token:
            revoked = bool(c.provider.revoke(access_token=token, spec=spec))
    except Exception:
        revoked = False                      # provider revoke is best-effort; local revoke stands
    conn.status = REVOKED
    conn.access_sealed = conn.refresh_sealed = ""     # credentials destroyed at rest
    await ConnectionStore(c.datastore).save(conn)
    await build_tokens(request).bump(conn.tenant)      # cached token dies immediately
    from app.core.token_manager import evict_lock
    evict_lock(conn.id)                                 # release the per-connection refresh lock (r2-07)
    return {"disconnected": True, "provider_revoked": revoked}


# ── sync ───────────────────────────────────────────────────────────────────────
@router.post("/sync")
async def sync(request: Request, body: SyncIn, principal: Principal = Depends(_ADMIN)) -> dict:
    conn = await _owned(request, body.connection_id, principal)
    spec = await _spec_for(request, conn)
    engine = build_sync(request)
    field_map = spec.get("field_map") or {}
    await request.app.state.auditor.record(
        "connector.sync_started", actor=principal.subject, tenant=principal.tenant or "",
        resource=f"connection:{conn.id}", ip=request.client.host if request.client else "",
        metadata={"direction": body.direction, "resource": body.resource})
    if body.direction == "pull":
        return await engine.pull(connection_id=conn.id, resource=body.resource, spec=spec,
                                 field_map=field_map)
    return await engine.push(connection_id=conn.id, resource=body.resource,
                             records=body.records, spec=spec, field_map=field_map)


@router.get("/runs", dependencies=[Depends(_READ)])
async def list_runs(request: Request, principal: Principal = Depends(_READ)) -> dict:
    return {"runs": await RunStore(request.app.state.container.datastore).list(
        _tenant(principal), get_settings().CONNECTOR_LIST_LIMIT)}


@router.get("/dead-letters", dependencies=[Depends(_READ)])
async def dead_letters(request: Request, principal: Principal = Depends(_READ)) -> dict:
    rows = await DlqStore(request.app.state.container.datastore).list(
        _tenant(principal), get_settings().CONNECTOR_LIST_LIMIT)
    # REDACT the raw provider payload: the DLQ row stores the full unmapped record, which routinely
    # carries PII/secrets. Reprocessing uses the stored row server-side; a human listing dead-letters
    # needs the metadata (what failed and why), not the customer's data (connector-r2-06).
    redacted = [{k: v for k, v in r.items() if k != "record"} for r in rows]
    return {"dead_letters": redacted}
