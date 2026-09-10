# =============================================================================
# assistant-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations


from pydantic import BaseModel, Field

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from app.backup_manifest import BACKUP
from app.core import backup as _backup
from app.config import Settings, get_settings
from app.core.pii import subject_ref
from app.core.erasure import TemplateErasure
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


@router.post("/assistant/retention", dependencies=[Depends(require_internal_token)])
async def enforce_retention(request: Request) -> dict:
    """Apply CONVERSATION_RETENTION_DAYS (a scheduler calls this). 0 = keep until erased.

    Shipped with a cron caller in the chart, because a sweeper nobody calls is the same defect as
    no sweeper at all — and harder to spot, since the code review passes.
    """
    import datetime as _dt

    s = get_settings()
    store = request.app.state.container.conversations
    if store is None or s.CONVERSATION_RETENTION_DAYS <= 0:
        return {"conversations": 0, "messages": 0, "skipped": True}
    cutoff = (_dt.datetime.now(_dt.timezone.utc)
              - _dt.timedelta(days=s.CONVERSATION_RETENTION_DAYS)).isoformat()
    result = await store.enforce_retention(cutoff_iso=cutoff)
    # A retention purge is irreversible bulk deletion of personal data. Once the conversations are
    # gone, THIS audit line is the only thing that can prove a purge happened, when, and how many —
    # the same reasoning that makes subject.erased critical. Recorded AFTER, because the COUNT (the
    # whole point of the record) is only known once the sweep has run; the failure is NOT swallowed
    # (unlike the best-effort abstained line) so a purge that could not be recorded surfaces to the
    # scheduler instead of vanishing. Counts only — never a tenant, a subject, or any content.
    if result.get("conversations") or result.get("messages"):
        await request.app.state.auditor.record(
            "assistant.retention_purged", actor="system", tenant="",
            resource="conversations", ip=request.client.host if request.client else "",
            metadata={"conversations": str(result.get("conversations", 0)),
                      "messages": str(result.get("messages", 0)),
                      "retention_days": str(s.CONVERSATION_RETENTION_DAYS)})
    return result


class TriageIn(BaseModel):
    tenant: str = ""
    current_type: str = ""
    subject: str = ""
    body: str = ""
    candidate_types: list[str] = Field(default_factory=list)


class DraftIn(BaseModel):
    tenant: str = ""
    subject: str = ""
    thread: list[dict] = Field(default_factory=list)


@router.post("/assistant/triage", dependencies=[Depends(require_internal_token)])
async def internal_triage(request: Request, body: TriageIn) -> dict:
    """Customer-service AI: suggest a type/priority/tags/summary for a new ticket. ADVISORY — an
    empty object means 'no suggestion' (no generative model wired, or nothing usable back), which the
    caller (ticket-service) treats as a shrug. Enabled only when MODEL_DRIVER is a real LLM."""
    from app.core import support
    return await support.triage(
        request.app.state.container.model, current_type=body.current_type, subject=body.subject,
        body=body.body, candidate_types=tuple(body.candidate_types),
        lang=get_settings().DEFAULT_LANGUAGE)


@router.post("/assistant/draft", dependencies=[Depends(require_internal_token)])
async def internal_draft(request: Request, body: DraftIn) -> dict:
    """Customer-service AI: draft a reply an agent reviews before sending. ADVISORY (see triage)."""
    from app.core import support
    return await support.draft(
        request.app.state.container.model, subject=body.subject, thread=body.thread,
        lang=get_settings().DEFAULT_LANGUAGE)


@router.post("/privacy/erase", dependencies=[Depends(require_internal_token)])
async def erase_subject(request: Request, body: EraseIn) -> dict:
    """Erase a data subject. Called by compliance-service during a DSAR fan-out.

    Returns the ack: what was erased, and what was RETAINED with its legal basis. Idempotent — a
    DSAR retries, so erasing an already-erased subject must not fail. Wire this service into the
    fan-out by adding it to PRIVACY_TARGETS in compliance-service's values file; the
    safety:privacy-wiring gate fails if you implement this and forget.
    """
    # Write-ahead: a critical action the log cannot take must not happen at all, so this runs
    # BEFORE the mutation. Recording afterwards would 503 a caller whose erasure had already
    # succeeded, and leave the one irreversible action in the service unproven.
    #
    # The resource carries the PSEUDONYM rather than the subject: an erasure record that names the
    # person it erased defeats the erasure.
    ref = subject_ref(get_settings().PII_SALT, body.tenant, body.subject)
    await request.app.state.auditor.record(
        "subject.erased", actor="", tenant=body.tenant, resource=f"subject:{ref}",
        ip=request.client.host if request.client else "")
    c = request.app.state.container
    s = get_settings()
    # Conversations are this service's own personal data; the eraser owns their deletion + accounting,
    # so the ack it returns is complete on its own (no separate merge that could drift).
    eraser = TemplateErasure(store=c.datastore, pii_salt=s.PII_SALT,
                             conversations=c.conversations, max_scan=s.ERASURE_MAX_SCAN)
    return await eraser.erase(body.tenant, body.subject)


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── AI connections (phase 3): N isolated LLM lines per tenant, each priced on its own ─────────
def _connections(request: Request):
    import uuid as _uuid
    from app.core.connections import ConnectionStore
    return ConnectionStore(request.app.state.container.datastore,
                           now=_now_iso, new_id=lambda: _uuid.uuid4().hex)


def _conn_secret_name(cid: str) -> str:
    return f"AI_CONNECTION_KEY:{cid}"


def _vertex_source(request: Request, cid: str, secret: str):
    """One cached VertexTokenSource per connection, so its short-lived-token cache actually survives
    between calls. Keyed by connection id and rebuilt only when the secret rotates (a sha256
    fingerprint mismatch), so the cache holds at most one live source per connection."""
    import hashlib
    from app.core.gcp_token import VertexTokenSource
    cache = request.app.state.__dict__.setdefault("_vertex_sources", {})
    fp = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    prev = cache.get(cid)
    if prev is None or prev[0] != fp:
        src = VertexTokenSource(sa_json=secret)
        cache[cid] = (fp, src)
        return src
    return prev[1]


def _conn_model(request: Request, conn, priority: bool = False):
    """A model bound to THIS connection's endpoint + credential — the isolation boundary. Its
    credential is the connection's own secret: an API key (Bearer), or an SA JSON minted into a
    Vertex OAuth token when auth='vertex'. `priority` (opt-in, double-locked) selects the Vertex
    priority tier via extra headers; see _priority_headers."""
    from app.adapters.model.http_provider import HttpModel
    store = getattr(request.app.state, "secret_store", None)

    async def resolver() -> str:
        secret = await store.get(tenant=conn.tenant, name=_conn_secret_name(conn.id),
                                 fallback="") if store is not None else ""
        if conn.auth == "vertex":
            # Reuse ONE VertexTokenSource per connection: it caches the OAuth token until just
            # before expiry, but that cache lives on the instance — building a fresh one per call
            # (the previous behaviour) threw it away every time, so every question signed a JWT and
            # made a token-endpoint round trip on the hot path, and hammered Google's token quota.
            # Rebuilt only when the connection's secret actually rotates (fingerprint mismatch).
            return await _vertex_source(request, conn.id, secret).token()
        return secret

    # For the anthropic shape, a Vertex connection is Claude-on-Vertex (:rawPredict) — its body needs
    # anthropic_version and the model is in the URL; a non-Vertex one is the direct Anthropic API
    # (x-api-key). Derived from (shape, auth), so no extra connection field is needed.
    anthropic_version = "vertex-2023-10-16" if (conn.shape == "anthropic"
                                                and conn.auth == "vertex") else ""
    return HttpModel(conn.url, "", model=conn.model, shape=conn.shape,
                     temperature=conn.temperature, key_resolver=resolver,
                     anthropic_version=anthropic_version,
                     extra_headers=_priority_headers(request, conn, priority))


def _priority_headers(request: Request, conn, priority: bool) -> dict[str, str]:
    """Vertex priority-tier headers, sent ONLY when BOTH locks are open and the connection is Vertex.

    Lock 1 (platform): ASSISTANT_PRIORITY_TIER_ENABLED — the capability is inert until the platform
    turns it on, so the ~1.8x surcharge can never be incurred by accident. Lock 2 (per call): the
    caller set priority=true on THIS invoke. Non-Vertex connections ignore it (the header is a Vertex
    concept). Verified vs Google Cloud docs (2026-09-10): for a no-Provisioned-Throughput project,
    'X-Vertex-AI-LLM-Request-Type: shared' + 'X-Vertex-AI-LLM-Shared-Request-Type: priority' selects
    Priority PayGo. Empty dict ⇒ the request is byte-identical to the standard tier."""
    if not (priority and conn.auth == "vertex"):
        return {}
    if not get_settings().ASSISTANT_PRIORITY_TIER_ENABLED:
        return {}
    return {"X-Vertex-AI-LLM-Request-Type": "shared",
            "X-Vertex-AI-LLM-Shared-Request-Type": "priority"}


def _conn_caller(request: Request, conn, priority: bool = False):
    """The text-completion caller for a tool-less connection (unchanged single-shot path)."""
    model = _conn_model(request, conn, priority)

    async def caller(system: str, user: str, max_tokens: int):
        return await model.complete(system=system, user=user, max_tokens=max_tokens)
    return caller


class ConnIn(BaseModel):
    tenant: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2048)
    shape: str = Field(default="chat", max_length=16)         # chat | generate | anthropic
    model: str = Field(default="", max_length=200)
    auth: str = Field(default="api_key", max_length=16)       # api_key | vertex
    system_prompt: str = Field(default="", max_length=20000)
    temperature: float = 0.2
    input_price_micros: int = Field(default=0, ge=0)          # micros per 1000 input tokens
    output_price_micros: int = Field(default=0, ge=0)
    currency: str = Field(default="SAR", max_length=8)
    # Optional per-connection tool/function declarations ({name, description, parameters}). Empty by
    # default → the connection is text-only exactly as before. Validated in core (a clean 422).
    tools: list[dict] = Field(default_factory=list)
    # The connection's credential — an API key, or (auth=vertex) a service-account JSON. Write-only:
    # stored in the secret store, never returned by any read route.
    credential: str = Field(default="", max_length=16000)


@router.post("/assistant/connections", dependencies=[Depends(require_internal_token)])
async def create_connection(request: Request, body: ConnIn) -> dict:
    """Declare a new isolated AI line for a tenant. Its credential is sealed in the secret store."""
    from app.core.connections import ConnectionError
    try:
        conn = await _connections(request).create(
            tenant=body.tenant, name=body.name, url=body.url, shape=body.shape, model=body.model,
            auth=body.auth, system_prompt=body.system_prompt, temperature=body.temperature,
            input_price_micros=body.input_price_micros, output_price_micros=body.output_price_micros,
            currency=body.currency, tools=body.tools)
    except ConnectionError as exc:
        raise HTTPException(422, str(exc)) from exc
    if body.credential:
        store = getattr(request.app.state, "secret_store", None)
        if store is None:
            raise HTTPException(503, "SECRETS_UNAVAILABLE — a credential needs SECRETS_KEK set")
        await store.set(tenant=body.tenant, name=_conn_secret_name(conn.id), value=body.credential)
    await request.app.state.auditor.record(
        "assistant.connection_created", actor="", tenant=body.tenant,
        resource=f"ai_connection:{conn.id}", ip=request.client.host if request.client else "",
        metadata={"auth": body.auth, "shape": body.shape})
    return conn.public()


@router.get("/assistant/connections", dependencies=[Depends(require_internal_token)])
async def list_connections(request: Request, tenant: str) -> dict:
    return {"connections": await _connections(request).list(tenant)}


@router.get("/assistant/connections/{cid}", dependencies=[Depends(require_internal_token)])
async def get_connection(request: Request, cid: str, tenant: str) -> dict:
    conn = await _connections(request).get(tenant, cid)
    if conn is None:
        raise HTTPException(404, "NOT_FOUND")
    return conn.public()


class ConnUpdateIn(BaseModel):
    tenant: str = Field(min_length=1, max_length=128)
    name: str | None = Field(default=None, max_length=200)
    url: str | None = Field(default=None, max_length=2048)
    shape: str | None = Field(default=None, max_length=16)
    model: str | None = Field(default=None, max_length=200)
    auth: str | None = Field(default=None, max_length=16)
    system_prompt: str | None = Field(default=None, max_length=20000)
    temperature: float | None = None
    input_price_micros: int | None = Field(default=None, ge=0)
    output_price_micros: int | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, max_length=8)
    tools: list[dict] | None = None


@router.put("/assistant/connections/{cid}", dependencies=[Depends(require_internal_token)])
async def update_connection(request: Request, cid: str, body: ConnUpdateIn) -> dict:
    """Patch a connection in place (tools, prompt, prices, ...). The credential is untouched — it
    lives in the secret store; recreate the connection to rotate it."""
    from app.core.connections import ConnectionError
    changes = body.model_dump(exclude={"tenant"}, exclude_none=True)
    try:
        conn = await _connections(request).update(body.tenant, cid, changes)
    except ConnectionError as exc:
        raise HTTPException(422, str(exc)) from exc
    if conn is None:
        raise HTTPException(404, "NOT_FOUND")
    await request.app.state.auditor.record(
        "assistant.connection_updated", actor="", tenant=body.tenant,
        resource=f"ai_connection:{cid}", ip=request.client.host if request.client else "",
        metadata={"fields": sorted(changes.keys())})
    return conn.public()


@router.delete("/assistant/connections/{cid}", dependencies=[Depends(require_internal_token)])
async def delete_connection(request: Request, cid: str, tenant: str) -> dict:
    await request.app.state.auditor.record(
        "assistant.connection_deleted", actor="", tenant=tenant,
        resource=f"ai_connection:{cid}", ip=request.client.host if request.client else "")
    store = getattr(request.app.state, "secret_store", None)
    if store is not None:
        await store.clear(tenant=tenant, name=_conn_secret_name(cid))
    return {"deleted": await _connections(request).delete(tenant, cid)}


class ConnInvokeIn(BaseModel):
    tenant: str = Field(min_length=1, max_length=128)
    # A one-shot prompt (text-only), OR a running transcript for a tool-calling loop. Exactly one is
    # used: `messages` wins when present. A tool-declaring connection uses the conversation path so
    # the model can return a tool-call; a tool-less connection stays single-shot exactly as before.
    prompt: str = Field(default="", max_length=100000)
    messages: list[dict] | None = None
    max_tokens: int = Field(default=0, ge=0)
    # Opt-in Vertex priority tier for THIS call (default OFF = standard tier, today's behaviour). Only
    # takes effect on a Vertex connection AND when the platform lock ASSISTANT_PRIORITY_TIER_ENABLED
    # is on; otherwise ignored. Costs ~1.8x the standard per-token rate when active.
    priority: bool = False


@router.post("/assistant/connections/{cid}/invoke", dependencies=[Depends(require_internal_token)])
async def invoke_connection(request: Request, cid: str, body: ConnInvokeIn) -> dict:
    """Run ONE connection and meter its usage. Isolated: only this connection's config, credential,
    tools and usage ledger are touched — never another connection or the service's main model.

    A connection that declares tools (or a call that passes `messages`) runs the conversation path and
    may return {"kind":"tool_call","calls":[...]}; the caller executes the tool and invokes again with
    the result appended. A tool-less connection with a bare `prompt` returns text exactly as before."""
    from app.core.connections import invoke as _invoke
    from app.core.connections import invoke_conversation as _invoke_conv
    from app.ports.model import ModelError
    cstore = _connections(request)
    conn = await cstore.get(body.tenant, cid)
    if conn is None:
        raise HTTPException(404, "NOT_FOUND")
    try:
        if conn.tools or body.messages is not None:
            model = _conn_model(request, conn, body.priority)
            messages = body.messages if body.messages is not None else body.prompt
            if not messages:
                raise HTTPException(422, "prompt or messages is required")
            return await _invoke_conv(conn, messages=messages, converse=model.converse,
                                      store=cstore, max_tokens=body.max_tokens)
        if not body.prompt:
            raise HTTPException(422, "prompt or messages is required")
        result = await _invoke(conn, prompt=body.prompt, caller=_conn_caller(request, conn, body.priority),
                               store=cstore, max_tokens=body.max_tokens)
        return {"kind": "text", **result}      # uniform shape: kind alongside the existing text field
    except ModelError as exc:
        # A provider/transport failure is a clean 502 with no sensitive body, like the KB path.
        raise HTTPException(502, "MODEL_CALL_FAILED") from exc


@router.get("/assistant/connections/{cid}/usage", dependencies=[Depends(require_internal_token)])
async def connection_usage(request: Request, cid: str, tenant: str) -> dict:
    return await _connections(request).usage(tenant, cid)


# ── Backup (backup-service orchestrates; this service produces sealed archives) ──────────────
# What this service holds is declared in app/backup_manifest.py; how it is read lives in
# core/backup.py. backup-service never opens this database — it calls export/verify/import over the
# internal token and stores the sealed archive, never seeing the plaintext.


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
    """Produce a sealed, checksummed archive of this service's declared data."""
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


@router.post("/backup/import", dependencies=[Depends(require_internal_token)])
async def backup_import(request: Request, body: BackupImportIn,
                        settings: Settings = Depends(get_settings)) -> dict:
    """Restore an archive this service produced. Idempotent; leaves newer rows alone by default."""
    _, unseal = _sealer(settings)
    try:
        return await _backup.import_archive(
            request.app.state.container.datastore, body.archive,
            unseal=unseal, overwrite=body.overwrite, into_tenant=body.into_tenant,
            declaration=BACKUP)
    except _backup.BackupError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
