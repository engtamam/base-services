# =============================================================================
# payments-service — Application factory & lifespan
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
from app.api import internal_payments
from app.api.v1 import payments_public
from app.api.errors import register_error_handlers
from app.api.health import register_readiness_check
from app.api.v1 import router as v1_router
from app.api.v1.operator import router as operator_router
from app.config import get_settings
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
    settings.assert_internal_policy_ok()
    # A misconfigured sealing key must refuse to START, not fail deep on the first credential op.
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
    async def lifespan(app_: FastAPI):
        logger.info(
            "startup",
            extra={
                "service": settings.SERVICE_NAME,
                "version": settings.SERVICE_VERSION,
                "ports": container.describe(),
            },
        )
        # Background reconciliation / expiry sweep — the safety net behind the (email-registered,
        # unreliable) TuwaiqPay webhook. OFF by default: the task is created only when a deployment
        # opts in, so nothing changes unless enabled.
        sweep = None
        if settings.PAYMENTS_RECONCILE_SWEEP_ENABLED:
            from app.core.reconcile_sweep import build_sweep
            sweep = build_sweep(app_, settings, logger=get_logger("app.reconcile_sweep"))
            sweep.start()
            logger.info(
                "reconcile_sweep_started",
                extra={"interval_s": settings.PAYMENTS_RECONCILE_INTERVAL_SECONDS,
                       "min_age_s": settings.PAYMENTS_RECONCILE_MIN_AGE_SECONDS})
        try:
            yield
        finally:
            if sweep is not None:
                await sweep.stop()
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
    # RED metrics (Rate / Errors / Duration) for every request, grouped by route TEMPLATE
    # (/x/{id}, not /x/<real-id>) so cardinality stays bounded however much tenant data accrues, and
    # 404/unhandled paths are excluded so scanning can't inflate the series. instrument() writes into
    # the DEFAULT prometheus registry that the metrics.router above already serves via generate_latest(),
    # so the existing /metrics picks these up with no second endpoint — never .expose(), which would
    # register a competing /metrics.
    from prometheus_fastapi_instrumentator import Instrumentator
    Instrumentator().instrument(app)
    app.include_router(internal.router)
    app.include_router(internal_payments.router)
    app.include_router(payments_public.router)
    app.include_router(v1_router)
    # Operator controls: credentials and policy an operator changes after launch.
    app.include_router(operator_router)

    return app


# ASGI entry point: `uvicorn app.main:app`
app = create_app()
