# =============================================================================
# notification-service — Application factory & lifespan
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
    # A service that cannot witness its own privileged actions must not serve
    # traffic as if it can.
    settings.assert_audit_config_ok()
    settings.assert_pii_config_ok()
    settings.assert_cors_config_ok()
    # And a service that gates sending on consent must be able to ask.
    settings.assert_consent_config_ok()
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
        # Seed the templates this deployment ships with. Upsert, so it is safe on every boot and
        # a rolling restart cannot leave half a template set. Failure is logged and NOT fatal: a
        # datastore that is briefly unavailable at boot must not stop the service from starting
        # and then serving every template that already exists.
        seeds = settings.notify_seed_templates
        if seeds:
            from app.core.consent_guard import is_unmapped_marketing
            from app.core.models import TEMPLATE_TRANSACTIONAL, Template
            from app.core.stores import TemplateStore
            # FAIL CLOSED, BEFORE serving a single request: a seeded MARKETING template with no
            # consent-purpose mapping would send unchecked. Refuse to start rather than run with the
            # hole — a deploy that pairs a marketing template with a missing mapping is a
            # misconfiguration to fix, not to serve. (Transactional seeds are unaffected.)
            purposes = settings.notify_consent_purposes
            unmapped = [e.get("key") for e in seeds
                        if is_unmapped_marketing(str(e.get("key", "")),
                                                 str(e.get("category", TEMPLATE_TRANSACTIONAL)
                                                     or TEMPLATE_TRANSACTIONAL), purposes)]
            if unmapped:
                raise RuntimeError(
                    "marketing seed template(s) not mapped to a consent purpose "
                    f"(NOTIFY_CONSENT_PURPOSES): {unmapped} — a marketing send must be consent-gated")
            store = TemplateStore(container.datastore)
            seeded = 0
            for entry in seeds:
                try:
                    await store.upsert(Template(
                        key=entry["key"], locale=entry["locale"], subject=entry["subject"],
                        body=entry["body"],
                        category=str(entry.get("category", TEMPLATE_TRANSACTIONAL)
                                     or TEMPLATE_TRANSACTIONAL)))
                    seeded += 1
                except Exception as exc:      # noqa: BLE001 - boot must not die on one template
                    logger.error("template_seed_failed",
                                 extra={"key": entry.get("key"), "error": type(exc).__name__})
            logger.info("templates_seeded", extra={"count": seeded, "declared": len(seeds)})
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
    # Validated at boot: a malformed declaration stops the service rather than
    # surfacing months later as a hole in the log.
    app.state.auditor = build_auditor(settings, container)
    app.state.cwe_guard = cwe_guard

    # ── Operator-managed credentials and policy ────────────────────────────
    # Without a KEK there is nothing to seal with, so the store is not built and the operator
    # surface refuses with 503 rather than storing a credential in the clear. The pool still
    # resolves — from the environment — so behaviour is exactly what it was before.
    from app.container import make_provider_pool
    from app.core.runtime_config import RuntimeConfig
    from app.core.secret_store import SecretStore

    secret_store = None
    if settings.SECRETS_KEK:
        secret_store = SecretStore(
            container.datastore, settings.SECRETS_KEK,
            rotation_window_s=settings.SECRETS_ROTATION_WINDOW_SECONDS)
    app.state.secret_store = secret_store
    from app.runtime_policy import RUNTIME_POLICY
    app.state.runtime_config = RuntimeConfig(
        container.datastore, Settings, policy=RUNTIME_POLICY)
    app.state.provider_pool = make_provider_pool(
        settings, secret_store, app.state.runtime_config)

    # ── WhatsApp booking Flow handler ───────────────────────────────────────────
    # Wire the config-driven booking engine as the Flow data-endpoint handler. Inert unless a
    # definition is configured (the engine reports "not configured"); the backend is the platform's
    # own service over a config URL, or the null backend when none is set. Single-brand for now
    # (tenant resolved as ""); per-WABA keying + hot per-tenant definitions come with provisioning.
    import time as _time

    from app.adapters.booking.config_defs import ConfigDefinitionProvider
    from app.adapters.booking.http import HttpBookingBackend
    from app.adapters.booking.null import NullBookingBackend
    from app.core.booking_flow import BookingEngine, SessionStore
    from app.core.flow_registry import FlowRegistry
    _backend = (HttpBookingBackend(settings.WHATSAPP_BOOKING_PROVIDER_URL,
                                   settings.WHATSAPP_BOOKING_PROVIDER_TOKEN)
                if settings.WHATSAPP_BOOKING_PROVIDER_URL else NullBookingBackend())
    _sessions = SessionStore(container.datastore, clock=_time.time,
                             ttl_seconds=settings.WHATSAPP_BOOKING_SESSION_TTL_SECONDS)
    _registry = FlowRegistry()
    _registry.register(BookingEngine(
        definitions=ConfigDefinitionProvider(settings.WHATSAPP_BOOKING_DEFINITION),
        availability=_backend, booking=_backend, sessions=_sessions))
    # Flow #1 — reschedule/cancel, on the SAME generic layer + booking screens. Talks to the SAME
    # platform bookings URL (op-based); null backend when unset.
    from app.adapters.reschedule.http import HttpReschedulingBackend
    from app.adapters.reschedule.null import NullReschedulingBackend
    from app.core.reschedule_flow import RescheduleEngine
    _resched = (HttpReschedulingBackend(settings.WHATSAPP_BOOKING_PROVIDER_URL,
                                        settings.WHATSAPP_BOOKING_PROVIDER_TOKEN)
                if settings.WHATSAPP_BOOKING_PROVIDER_URL else NullReschedulingBackend())
    _registry.register(RescheduleEngine(provider=_resched, sessions=_sessions))
    # Flow #6 — loyalty points redemption, same generic layer + booking screens. Its OWN config URL
    # (loyalty is usually a separate system); null backend when unset.
    from app.adapters.loyalty.http import HttpLoyaltyBackend
    from app.adapters.loyalty.null import NullLoyaltyBackend
    from app.core.loyalty_flow import LoyaltyEngine
    _loyalty = (HttpLoyaltyBackend(settings.WHATSAPP_LOYALTY_PROVIDER_URL,
                                   settings.WHATSAPP_LOYALTY_PROVIDER_TOKEN)
                if settings.WHATSAPP_LOYALTY_PROVIDER_URL else NullLoyaltyBackend())
    _registry.register(LoyaltyEngine(provider=_loyalty, sessions=_sessions))
    # Flow #6b — SUBSCRIPTION RENEWAL over WhatsApp, the same generic redeem-a-thing engine with a
    # second name + its own provider URL (usually billing-service): it lists the tenant's renewable
    # subscriptions as "rewards" and "redeems" one by taking the renewal payment. Its own config URL;
    # null backend (flow simply unavailable) when unset — never a fake renewal.
    _renewal = (HttpLoyaltyBackend(settings.WHATSAPP_RENEWAL_PROVIDER_URL,
                                   settings.WHATSAPP_RENEWAL_PROVIDER_TOKEN)
                if settings.WHATSAPP_RENEWAL_PROVIDER_URL else NullLoyaltyBackend())
    _registry.register(LoyaltyEngine(provider=_renewal, sessions=_sessions,
                                     name="subscription_renewal"))
    # Flow #2 — delivery slots IS the slot engine (a second BookingEngine, name "delivery"), its own
    # definition + provider URL. options=zone, calendar=delivery days, slots=capacity-limited windows.
    _delivery_backend = (HttpBookingBackend(settings.WHATSAPP_DELIVERY_PROVIDER_URL,
                                            settings.WHATSAPP_DELIVERY_PROVIDER_TOKEN)
                         if settings.WHATSAPP_DELIVERY_PROVIDER_URL else NullBookingBackend())
    _registry.register(BookingEngine(
        name="delivery",
        definitions=ConfigDefinitionProvider(settings.WHATSAPP_DELIVERY_DEFINITION),
        availability=_delivery_backend, booking=_delivery_backend, sessions=_sessions))
    # Visit registration — the SAME slot-reservation engine (name "visit") for a "happens now" record:
    # free-text (phone) + multi-select (services) + confirm, no calendar. Its own definition + URL;
    # null backend when unset. Companion Flow JSON at GET /internal/whatsapp/flow/visit-json.
    _visit_backend = (HttpBookingBackend(settings.WHATSAPP_VISIT_PROVIDER_URL,
                                         settings.WHATSAPP_VISIT_PROVIDER_TOKEN)
                      if settings.WHATSAPP_VISIT_PROVIDER_URL else NullBookingBackend())
    _registry.register(BookingEngine(
        name="visit",
        definitions=ConfigDefinitionProvider(settings.WHATSAPP_VISIT_DEFINITION),
        availability=_visit_backend, booking=_visit_backend, sessions=_sessions))
    # Flow #3 — order/request status (read-only). Its own config URL; null backend when unset.
    from app.adapters.order_status.http import HttpOrderStatusBackend
    from app.adapters.order_status.null import NullOrderStatusBackend
    from app.core.order_status_flow import OrderStatusEngine
    _status_backend = (HttpOrderStatusBackend(settings.WHATSAPP_ORDER_STATUS_PROVIDER_URL,
                                              settings.WHATSAPP_ORDER_STATUS_PROVIDER_TOKEN)
                       if settings.WHATSAPP_ORDER_STATUS_PROVIDER_URL else NullOrderStatusBackend())
    _registry.register(OrderStatusEngine(provider=_status_backend))
    app.state.flow_registry = _registry

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
    # RED metrics into the DEFAULT registry the /metrics route already serves — instrument()
    # only, never expose(). Grouped by route template (bounded cardinality); see _template.
    from prometheus_fastapi_instrumentator import Instrumentator
    Instrumentator().instrument(app)
    # Public, unauthenticated by a token — the inbound Meta webhook's auth IS its signature (POST) and
    # the shared verify token (GET). Mounted with the other public routers, before the internal tier.
    from app.api import webhooks
    app.include_router(webhooks.router)
    app.include_router(internal.router)
    app.include_router(v1_router)
    # Operator controls: credentials and policy an operator changes after launch.
    app.include_router(operator_router)

    return app


# ASGI entry point: `uvicorn app.main:app`
app = create_app()
