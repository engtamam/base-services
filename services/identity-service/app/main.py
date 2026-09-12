# =============================================================================
# identity-service — Application factory & lifespan
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

import asyncio
import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import health, internal, jwks as jwks_api, metrics
from app.api.errors import register_error_handlers
from app.api.health import register_readiness_check
from app.api.v1 import router as v1_router
from app.api.v1.operator import router as operator_router
from app.api.v1.tenancy import members_router, router as tenancy_router
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


async def _refresh_signing_keys(sks, logger) -> None:
    """Periodically re-read the sealed keyring so a rotation performed on another replica (or by the
    cron hitting a different pod) is picked up here. Never raises out — a bad read keeps the last
    good ring rather than dropping the ability to sign."""
    while True:
        try:
            await asyncio.sleep(sks.refresh_seconds)
            await sks.reload()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("signing_key_refresh_error", exc_info=True)


def create_app() -> FastAPI:
    # Fail-fast: this raises (and aborts startup) if the environment is invalid.
    settings = get_settings()
    # A service that cannot witness its own privileged actions must not serve
    # traffic as if it can.
    settings.assert_audit_config_ok()
    settings.assert_pii_config_ok()
    settings.assert_cors_config_ok()
    # And a service that cannot pseudonymise must not claim it can erase anyone.
    # And a service that demands a proven address must be able to ask for proof.
    settings.assert_email_verification_ok()
    settings.assert_operator_config_ok()
    # The platform-capability boundary must be decidable: multi-tenant membership without a
    # PLATFORM_TENANT (and without an explicit opt-out) fails the mint-time '*'/'platform.*' strip
    # open, so refuse to boot rather than mint cross-tenant authority (audit identity-r2-05).
    settings.assert_platform_boundary_ok()
    # Enabling signing-key rotation without its prerequisites (rs256, a KEK, a seed key, an overlap
    # that outlives the refresh TTL) must stop the service, not degrade quietly.
    settings.assert_signing_rotation_ok()
    settings.assert_legal_enforcement_ok()
    # A misspelled onboarding method must stop the service, not silently disable a signup path.
    from app.core.onboarding import build_onboarding_policy
    build_onboarding_policy(settings)
    configure_logging(settings.SERVICE_NAME, settings.ENV, settings.LOG_LEVEL)
    logger = get_logger("app.main")

    # An UNSET platform tenant means platform.* is still only a naming convention here: any
    # capability that reaches a User becomes platform authority on login. That is the behaviour
    # this service always had, so it is not forced — but it is an assertion, and an assertion
    # nobody states is one nobody revisits. Same reasoning as CONSENT_AGE_OF_MAJORITY=0.
    if not settings.PLATFORM_TENANT:
        logger.warning(
            "platform_tenant_unset",
            extra={"detail": "PLATFORM_TENANT is empty: platform.* and '*' capabilities are NOT "
                             "restricted to a platform tenant. Set it to enforce the boundary."})

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
        # Signing-key rotation: load the sealed keyring (seeding it from TOKEN_PRIVATE_KEY on first
        # boot) and keep it fresh so a rotation done on another replica is picked up here too. Done
        # inside the lifespan because it is async; the issuer was already pointed at the service's
        # in-memory current key during composition, so it starts serving as soon as load() returns.
        refresh_task = None
        sks = getattr(_app.state, "signing_key_service", None)
        if sks is not None:
            await sks.load()
            logger.info("signing_key_rotation_active",
                        extra={"detail": f"jwks refresh every {int(sks.refresh_seconds)}s"})
            refresh_task = asyncio.create_task(_refresh_signing_keys(sks, logger))
        yield
        if refresh_task is not None:
            refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await refresh_task
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
    # Validated at boot: a malformed declaration stops the service rather than
    # surfacing months later as a hole in the log.
    app.state.auditor = build_auditor(settings, container)
    app.state.cwe_guard = cwe_guard

    # ── Operator-managed credentials and policy ────────────────────────────
    # Without a KEK there is nothing to seal with, so the store is not built and the operator
    # surface refuses with 503 rather than storing a credential in the clear. Resolution still
    # happens — from the environment — so behaviour is exactly what it was before.
    from app.core.operator_credentials import (AUTHZ_INTERNAL_TOKEN, COMPLIANCE_INTERNAL_TOKEN,
                                               OperatorCredentials)
    from app.core.runtime_config import RuntimeConfig
    from app.core.secret_store import SecretStore
    from app.runtime_policy import RUNTIME_POLICY

    secret_store = None
    if settings.SECRETS_KEK:
        secret_store = SecretStore(
            container.datastore, settings.SECRETS_KEK,
            rotation_window_s=settings.SECRETS_ROTATION_WINDOW_SECONDS)
    app.state.secret_store = secret_store
    # ── Automatic signing-key rotation ─────────────────────────────────────
    # When enabled (and only with a KEK to seal the ring and an rs256 issuer to swap), point the
    # issuer at the keyring's in-memory current key. The ring itself is loaded async in the lifespan;
    # until then the service is present but unloaded, and no token is minted before startup finishes.
    app.state.signing_key_service = None
    if settings.TOKEN_KEY_ROTATION_ENABLED:
        from app.adapters.token_issuer.rs256 import RS256Issuer
        from app.core.signing_key_service import SigningKeyService

        overlap = (settings.TOKEN_SIGNING_OVERLAP_SECONDS
                   or settings.REFRESH_TTL_SECONDS + 3600)
        sks = SigningKeyService(
            secret_store, overlap_seconds=overlap, seed_pem=settings.TOKEN_PRIVATE_KEY,
            refresh_seconds=settings.TOKEN_KEY_REFRESH_SECONDS,
            min_age_seconds=settings.TOKEN_KEY_ROTATION_MIN_AGE_SECONDS)
        app.state.signing_key_service = sks
        container.token_issuer = RS256Issuer(
            issuer=settings.TOKEN_ISSUER, audience=settings.TOKEN_AUDIENCE,
            key_provider=sks.current_signing_key)
    app.state.runtime_config = RuntimeConfig(
        container.datastore, Settings, policy=RUNTIME_POLICY)
    app.state.operator_credentials = OperatorCredentials(
        secret_store, ttl_s=settings.AUTHZ_CREDENTIAL_REFRESH_SECONDS)
    # The caps resolver captured its token at construction, which is why changing it needed a
    # redeploy. It now asks at mint time, with the configured value as the fallback.
    caps_resolver = getattr(container, "caps_resolver", None)
    if hasattr(caps_resolver, "set_token_provider"):
        creds = app.state.operator_credentials
        fallback = settings.AUTHZ_INTERNAL_TOKEN
        caps_resolver.set_token_provider(
            lambda: creds.get(AUTHZ_INTERNAL_TOKEN, fallback=fallback))
    # Same rotation-aware seam for the legal gate's compliance token (no-op for the `off` gate).
    legal_gate = getattr(container, "legal_gate", None)
    if hasattr(legal_gate, "set_token_provider"):
        creds = app.state.operator_credentials
        legal_fallback = settings.COMPLIANCE_INTERNAL_TOKEN
        legal_gate.set_token_provider(
            lambda: creds.get(COMPLIANCE_INTERNAL_TOKEN, fallback=legal_fallback))

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

    # Refund a counted-surface request whose final status did not count (T6). No-op unless
    # RATELIMIT_COUNT_STATUSES is set, so it costs nothing for services that do not use it.
    from app.api.ratelimit import build_policy, register_ratelimit_refund
    app.state.ratelimit_policy = build_policy(settings)
    register_ratelimit_refund(app)

    # ── Routers: public -> internal -> user ─────────────────────────────────
    app.include_router(health.router)
    app.include_router(metrics.router)
    # RED metrics into the DEFAULT registry the /metrics route already serves — instrument()
    # only, never expose(). Grouped by route template (bounded cardinality); see _template.
    from prometheus_fastapi_instrumentator import Instrumentator
    Instrumentator().instrument(app)
    # Public JWKS: the verify half of the signing keyring, unauthenticated by design.
    app.include_router(jwks_api.router)
    app.include_router(internal.router)
    app.include_router(v1_router)
    # Operator controls: credentials and policy an operator changes after launch.
    app.include_router(operator_router)
    # Multi-tenant membership (Phase 31) is opt-in. Off — the default — these routes do not exist
    # at all and the deployment 404s them, rather than exposing a feature it has no store for.
    if settings.IDENTITY_MULTI_TENANT:
        app.include_router(tenancy_router)
        app.include_router(members_router)
    # Machine identity (OAuth2 client-credentials) is opt-in. Included only when enabled, so a
    # deployment that has not turned it on has no token-exchange endpoint at all.
    if settings.SERVICE_ACCOUNTS_ENABLED:
        from app.api.v1.service_accounts import router as service_accounts_router
        app.include_router(service_accounts_router)

    return app


# ASGI entry point: `uvicorn app.main:app`
app = create_app()
