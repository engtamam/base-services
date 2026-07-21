# =============================================================================
# assistant-service — Application factory & lifespan
# -----------------------------------------------------------------------------
# Builds the FastAPI app: validated config (fail-fast), structured logging,
# hardened middleware, safe error handlers, and the three endpoint tiers
# (public / internal / user).
#
# Middleware nesting (outermost -> innermost), so every response — including
# errors and timeouts — is tagged and hardened:
#     RequestId  ->  SecurityHeaders  ->  Timeout  ->  BodyLimit  ->  app
# Starlette runs the LAST-added middleware outermost, so we add them in the
# reverse of that order below.
# =============================================================================
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import health, internal, metrics
from app.api.errors import register_error_handlers
from app.api.health import register_readiness_check
from app.api.v1 import router as v1_router
from app.api.v1.assistant import router as assistant_router
from app.api.v1.operator import router as operator_router
from app.config import Settings, get_settings
from app.core.audit import build_auditor
from app.container import build_container
from app.middleware import (
    BodyLimitMiddleware,
    RequestIdMiddleware,
    SecurityHeadersMiddleware,
    TimeoutMiddleware,
)
from app.observability.logging import configure_logging, get_logger
from app.security.catalog import get_catalog
from app.security.checks import build_checks
from app.security.cwe_guard import CweGuard
from app.security.step_map import load_step_map


def create_app() -> FastAPI:
    # Fail-fast: this raises (and aborts startup) if the environment is invalid.
    settings = get_settings()
    # A service that cannot witness its own privileged actions must not serve traffic as if it can.
    settings.assert_audit_config_ok()
    settings.assert_pii_config_ok()
    settings.assert_cors_config_ok()
    settings.assert_secrets_config_ok()
    configure_logging(settings.SERVICE_NAME, settings.ENV, settings.LOG_LEVEL)
    logger = get_logger("app.main")

    # Composition root: wire ports -> adapters from config (fail-fast on bad driver).
    container = build_container(settings)

    # CWE self-guard: pre/post-step checks referencing the bundled MITRE catalog.
    cwe_guard = CweGuard(
        catalog=get_catalog(),
        step_map=load_step_map(settings.CWE_STEP_MAP_FILE),
        checks=build_checks(settings),
        enabled=settings.CWE_GUARD_ENABLED,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        logger.info(
            "startup",
            extra={
                "service": settings.SERVICE_NAME,
                "version": settings.SERVICE_VERSION,
                "ports": container.describe(),
            },
        )
        yield
        await container.close()
        logger.info("shutdown", extra={"service": settings.SERVICE_NAME})

    app = FastAPI(
        title=settings.SERVICE_NAME,
        version=settings.SERVICE_VERSION,
        lifespan=lifespan,
        # Docs off by default in production (reduce surface); on elsewhere.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )
    app.state.container = container
    # Validated at boot: a malformed declaration stops the service rather than surfacing months
    # later as a hole in the log.
    app.state.auditor = build_auditor(settings, container)
    app.state.cwe_guard = cwe_guard

    # ── Operator-managed credentials and policy ────────────────────────────
    # An operator has to be able to point the assistant at a different LLM provider, or rotate the
    # key of the one it already uses, after launch. Without a KEK there is nothing to seal with, so
    # the store is not built and the operator surface refuses with 503 rather than storing a
    # credential in the clear. The credential cache still resolves — from the environment — so
    # behaviour is exactly what it was before.
    from app.core.model_credentials import ModelCredentials
    from app.core.runtime_config import RuntimeConfig
    from app.runtime_policy import RUNTIME_POLICY
    from app.core.secret_store import SecretStore

    secret_store = None
    if settings.SECRETS_KEK:
        secret_store = SecretStore(
            container.datastore, settings.SECRETS_KEK,
            rotation_window_s=settings.SECRETS_ROTATION_WINDOW_SECONDS)
    app.state.secret_store = secret_store
    app.state.runtime_config = RuntimeConfig(
        container.datastore, Settings, policy=RUNTIME_POLICY)
    app.state.model_credentials = ModelCredentials(
        settings, secret_store, ttl_s=settings.MODEL_CREDENTIAL_REFRESH_SECONDS)
    # Only the HTTP driver authenticates to anything; the extractive one has no credential and
    # must not grow a code path that pretends otherwise.
    if hasattr(container.model, "use_key_resolver"):
        container.model.use_key_resolver(app.state.model_credentials.get)

    # Readiness reflects real dependency health for the wired adapters.
    for name, probe in container.health_probes().items():
        register_readiness_check(name, probe)

    # ── Middleware (added inner -> outer; see header note) ──────────────────
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.BODY_LIMIT_BYTES)
    app.add_middleware(TimeoutMiddleware, timeout_ms=settings.REQUEST_TIMEOUT_MS)
    app.add_middleware(
        SecurityHeadersMiddleware,
        frame_ancestors=settings.ALLOWED_FRAME_ANCESTORS,
        is_production=settings.is_production,
    )
    app.add_middleware(RequestIdMiddleware)

    # CORS is added LAST so it is the OUTERMOST middleware: a preflight must be answered before
    # the body limit, the timeout or anything else can reject it, and a rejected preflight is an
    # error the browser reports as a CORS failure rather than as the real reason.
    if settings.cors_origins:
        from fastapi.middleware.cors import CORSMiddleware
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=settings.CORS_ALLOW_CREDENTIALS,
            allow_methods=settings.cors_methods,
            allow_headers=settings.cors_headers,
            max_age=settings.CORS_MAX_AGE_SECONDS,
        )

    # ── Error handlers (safe, generic) ──────────────────────────────────────
    register_error_handlers(app)

    from app.api.ratelimit import build_policy, register_ratelimit_refund
    app.state.ratelimit_policy = build_policy(settings)
    register_ratelimit_refund(app)

    # ── Routers: public -> internal -> user ─────────────────────────────────
    app.include_router(health.router)
    app.include_router(metrics.router)
    app.include_router(internal.router)
    app.include_router(v1_router)
    # The assistant's own tier. Mounted separately from the template's v1 router so a
    # route here is obviously this service's product surface rather than scaffolding.
    app.include_router(assistant_router)
    # Operator controls: credentials and policy an operator changes after launch.
    app.include_router(operator_router)

    # The widget itself. Served UNAUTHENTICATED and deliberately: it is a static script that
    # contains no data and no secret — the host mints a token and passes it in. Requiring a token
    # to fetch the script would mean the page needs a token before it can load the thing that uses
    # the token.
    if settings.WIDGET_ENABLED:
        from fastapi.responses import FileResponse
        import pathlib as _pathlib

        _widget = _pathlib.Path(__file__).resolve().parent / "widget" / "widget.js"

        @app.get("/widget.js", include_in_schema=False)
        async def widget_js():                      # noqa: D401
            return FileResponse(
                _widget, media_type="application/javascript",
                headers={"Cache-Control": "public, max-age=300",
                         # It is a script, not a document: nothing here should ever be framed or
                         # sniffed into another type.
                         "X-Content-Type-Options": "nosniff"})

    return app


# ASGI entry point: `uvicorn app.main:app`
app = create_app()
