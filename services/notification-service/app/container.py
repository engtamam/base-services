# =============================================================================
# notification-service — Composition root (config-driven port -> adapter wiring)
# -----------------------------------------------------------------------------
# The registry is the ALLOWLIST: only listed drivers can be constructed, so an
# unknown/injected driver is impossible (fail-fast ConfigError at boot). Adapter
# selection is pure config (zero-hardcoded). The core depends on PORTS; only this
# file knows the concrete adapters.
# =============================================================================
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

_log = logging.getLogger(__name__)

from app.adapters.cache.memory import MemoryCache
from app.adapters.cache.redis import RedisCache
from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.datastore.postgres import PostgresDataStore
from app.adapters.event_bus.memory import MemoryEventBus
from app.adapters.event_bus.redis import RedisEventBus
from app.adapters.object_store.local import LocalObjectStore
from app.adapters.object_store.memory import MemoryObjectStore
from app.adapters.object_store.s3 import S3ObjectStore
from app.adapters.queue.memory import MemoryQueue
from app.adapters.queue.redis import RedisQueue
from app.adapters.rate_limiter.memory import MemoryRateLimiter
from app.adapters.rate_limiter.redis import RedisRateLimiter
from app.adapters.secrets.env import EnvSecrets
from app.adapters.secrets.file import FileSecrets
from app.adapters.secrets.vault import VaultSecrets
from app.adapters.audit_sink.eventbus import EventBusAuditSink
from app.adapters.audit_sink.http import HttpAuditSink
from app.adapters.audit_sink.null import NullAuditSink
from app.adapters.consent_gate.http import HttpConsentGate
from app.adapters.consent_gate.null import NullConsentGate
from app.adapters.telemetry.stdout import StdoutTelemetry
from app.adapters.token_verifier.hs256 import HS256Verifier
from app.adapters.token_verifier.jwks import JWKSVerifier
from app.adapters.token_verifier.null import NullVerifier
from app.adapters.token_verifier.rs256 import RS256Verifier
from app.config import Settings
from app.ports.cache import Cache
from app.ports.datastore import DataStore
from app.ports.errors import ConfigError
from app.ports.event_bus import EventBus
from app.ports.object_store import ObjectStore
from app.ports.queue import Queue
from app.ports.rate_limiter import RateLimiter
from app.ports.secrets_provider import SecretsProvider
from app.ports.telemetry import Telemetry
from app.ports.token_verifier import TokenVerifier

Builder = Callable[[Settings], Any]

# ── Registries (the allowlist) — driver name -> builder ─────────────────────
_DATASTORE: dict[str, Builder] = {
    "memory": lambda s: MemoryDataStore(),
    "postgres": lambda s: PostgresDataStore(
        s.DATASTORE_DSN,
        pool_min=s.DATASTORE_POOL_MIN,
        pool_max=s.DATASTORE_POOL_MAX,
        pool_timeout=s.DATASTORE_POOL_TIMEOUT_SECONDS,
        pool_max_inactive_lifetime=s.DATASTORE_POOL_MAX_INACTIVE_LIFETIME_SECONDS,
        statement_cache_size=s.DATASTORE_STATEMENT_CACHE_SIZE,
    ),
}
_CACHE: dict[str, Builder] = {
    "memory": lambda s: MemoryCache(),
    "redis": lambda s: RedisCache(s.CACHE_URL),
}
_EVENTBUS: dict[str, Builder] = {
    "memory": lambda s: MemoryEventBus(),
    "redis": lambda s: RedisEventBus(s.EVENTBUS_URL),
}
_QUEUE: dict[str, Builder] = {
    "memory": lambda s: MemoryQueue(),
    "redis": lambda s: RedisQueue(s.QUEUE_URL),
}
_OBJECTSTORE: dict[str, Builder] = {
    "local": lambda s: LocalObjectStore(s.OBJECTSTORE_ROOT),
    "memory": lambda s: MemoryObjectStore(),
    "s3": lambda s: S3ObjectStore(
        s.OBJECTSTORE_BUCKET, s.OBJECTSTORE_ENDPOINT_URL,
        access_key=s.OBJECTSTORE_ACCESS_KEY, secret_key=s.OBJECTSTORE_SECRET_KEY,
        region=s.OBJECTSTORE_REGION, force_path_style=s.OBJECTSTORE_FORCE_PATH_STYLE),
}
_SECRETS: dict[str, Builder] = {
    "env": lambda s: EnvSecrets(),
    "file": lambda s: FileSecrets(s.SECRETS_FILE),
    "vault": lambda s: VaultSecrets(s.VAULT_ADDR, s.VAULT_TOKEN, s.VAULT_PATH),
}
_TOKEN: dict[str, Builder] = {
    "null": lambda s: NullVerifier(),
    "hs256": lambda s: HS256Verifier(s.TOKEN_HS256_SECRET, s.TOKEN_ISSUER, s.TOKEN_AUDIENCE),
    "rs256": lambda s: RS256Verifier(s.TOKEN_PUBLIC_KEY, s.TOKEN_ISSUER, s.TOKEN_AUDIENCE),
    "jwks": lambda s: JWKSVerifier(s.TOKEN_JWKS_URL, s.TOKEN_ISSUER, s.TOKEN_AUDIENCE),
}
_RATELIMITER: dict[str, Builder] = {
    "memory": lambda s: MemoryRateLimiter(s.RATELIMIT_MAX, s.RATELIMIT_WINDOW_SECONDS),
    "redis": lambda s: RedisRateLimiter(s.RATELIMIT_URL, s.RATELIMIT_MAX, s.RATELIMIT_WINDOW_SECONDS,
                                        degrade=s.RATELIMIT_DEGRADE),
}
# Where privileged actions are witnessed. `null` records nothing and SAYS so — the boot guard
# refuses it in production, because a chain that verifies perfectly while empty is a control that
# exists only on paper.
_AUDIT_SINK: dict[str, Builder] = {
    "null": lambda s: NullAuditSink(),
    "http": lambda s: HttpAuditSink(s.audit_ingest_url, s.INTERNAL_SERVICE_TOKEN,
                                    timeout=s.AUDIT_TIMEOUT_SECONDS,
                                    max_attempts=s.AUDIT_MAX_ATTEMPTS,
                                    backoff=s.AUDIT_RETRY_BACKOFF_SECONDS),
}
_CONSENT_GATE: dict[str, Builder] = {
    "null": lambda s: NullConsentGate(),
    "http": lambda s: HttpConsentGate(s.COMPLIANCE_SERVICE_URL, s.INTERNAL_SERVICE_TOKEN,
                                      timeout=s.CONSENT_TIMEOUT_SECONDS,
                                      path=s.CONSENT_CHECK_PATH),
}
_TELEMETRY: dict[str, Builder] = {
    "stdout": lambda s: StdoutTelemetry(),
}

# ── notification-service channels ───────────────────────────────────────────
from app.adapters.email.console import ConsoleEmail
from app.adapters.email.mailgun import MailgunEmail
from app.adapters.email.postmark import PostmarkEmail
from app.adapters.email.resend import ResendEmail
from app.adapters.email.sendgrid import SendgridEmail
from app.adapters.email.smtp import SmtpEmail
from app.adapters.failover import FailoverEmail, FailoverSms, FailoverWhatsApp
from app.adapters.renderer.safe import SafeRenderer
from app.adapters.sms.console import ConsoleSms
from app.adapters.sms.mobily import MobilySms
from app.adapters.sms.msegat import MsegatSms
from app.adapters.sms.taqnyat import TaqnyatSms
from app.adapters.sms.twilio import TwilioSms
from app.adapters.sms.unifonic import UnifonicSms
from app.adapters.sms.vonage import VonageSms
from app.adapters.sms.webhook import WebhookSms
from app.adapters.whatsapp.console import ConsoleWhatsApp
from app.adapters.whatsapp.meta import MetaWhatsApp
from app.adapters.whatsapp.webhook import WebhookWhatsApp
from app.adapters.whatsapp_flows.console import ConsoleFlowManager
from app.adapters.whatsapp_flows.meta import MetaFlowManager
from app.adapters.whatsapp_media.console import ConsoleMediaUploader
from app.adapters.whatsapp_media.meta import MetaMediaUploader
from app.adapters.whatsapp_templates.console import ConsoleTemplateManager
from app.adapters.whatsapp_templates.meta import MetaTemplateManager

_EMAIL_PROVIDERS: dict[str, Builder] = {
    "console": lambda s: ConsoleEmail(),
    "smtp": lambda s: SmtpEmail(s.SMTP_HOST, s.SMTP_PORT, s.SMTP_USER, s.SMTP_PASSWORD,
                               s.SMTP_STARTTLS, s.HTTP_TIMEOUT_SECONDS),
    "resend": lambda s: ResendEmail(s.RESEND_API_KEY),
    "sendgrid": lambda s: SendgridEmail(s.SENDGRID_API_KEY),
    "mailgun": lambda s: MailgunEmail(s.MAILGUN_API_KEY, s.MAILGUN_DOMAIN, s.MAILGUN_BASE),
    "postmark": lambda s: PostmarkEmail(s.POSTMARK_TOKEN, s.POSTMARK_STREAM),
}
_SMS_PROVIDERS: dict[str, Builder] = {
    "console": lambda s: ConsoleSms(),
    "twilio": lambda s: TwilioSms(s.TWILIO_ACCOUNT_SID, s.TWILIO_AUTH_TOKEN, s.TWILIO_FROM),
    "vonage": lambda s: VonageSms(s.VONAGE_API_KEY, s.VONAGE_API_SECRET, s.VONAGE_FROM),
    "unifonic": lambda s: UnifonicSms(s.UNIFONIC_APP_SID, s.UNIFONIC_SENDER),
    "taqnyat": lambda s: TaqnyatSms(s.TAQNYAT_TOKEN, s.TAQNYAT_SENDER),
    "msegat": lambda s: MsegatSms(s.MSEGAT_USERNAME, s.MSEGAT_API_KEY, s.MSEGAT_SENDER),
    "mobily": lambda s: MobilySms(s.MOBILY_MOBILE, s.MOBILY_PASSWORD, s.MOBILY_SENDER),
    "webhook": lambda s: WebhookSms(s.SMS_WEBHOOK_URL, s.SMS_WEBHOOK_BEARER),
}
_WHATSAPP_PROVIDERS: dict[str, Builder] = {
    "console": lambda s: ConsoleWhatsApp(),
    "meta": lambda s: MetaWhatsApp(s.WHATSAPP_ACCESS_TOKEN, s.WHATSAPP_PHONE_NUMBER_ID,
                                   s.WHATSAPP_GRAPH_BASE),
    "webhook": lambda s: WebhookWhatsApp(s.WHATSAPP_WEBHOOK_URL, s.WHATSAPP_WEBHOOK_BEARER),
}


def _build_chain(chain: list[str], registry: dict[str, Builder], name: str, settings):
    if not chain:
        chain = ["console"]
    providers = []
    for driver in chain:
        builder = registry.get(driver)
        if builder is None:
            # An UNKNOWN driver is a config TYPO, not a not-yet-set credential — still fatal, so a
            # misspelled provider is caught at deploy rather than silently dropped to console.
            raise ConfigError(
                f"unknown {name} provider: {driver!r} — allowed: {', '.join(sorted(registry))}"
            )
        try:
            providers.append(builder(settings))
        except ConfigError:
            # A KNOWN provider whose CREDENTIAL is not set yet (e.g. Meta WhatsApp on a fresh install,
            # before an admin has entered a token) must NOT crash the whole service at import/boot: that
            # turned one unconfigured optional channel into CrashLoopBackOff for the entire process, so
            # no channel and no route came up. Drop it and boot DEGRADED instead — the provider lights
            # up later without a redeploy, because the per-tenant send path already resolves credentials
            # at call time through _tolerant_chain. Same posture, just deferred from boot to send.
            _log.warning("provider_unconfigured_skipped_at_boot",
                         extra={"channel": name, "provider": driver})
    if not providers:
        # Console is the floor so the chain is never empty — an empty chain fails every send with no
        # provider even to name in the log. A missing token thus degrades to console, never to nothing.
        providers.append(registry["console"](settings))
        _log.warning("provider_chain_empty_fell_back_to_console", extra={"channel": name})
    return providers


class _Overlay:
    """Settings, with the fourteen provider credentials replaced by resolved ones.

    The builder lambdas above read `s.RESEND_API_KEY` and so on. Rather than rewriting all fourteen
    to take credentials another way — fourteen chances to miss one, and a miss looks like a working
    provider using a stale key — the resolved values are laid over the settings object. Everything
    else delegates untouched.
    """

    __slots__ = ("_s", "_over")

    def __init__(self, settings, over: dict[str, str]) -> None:
        self._s = settings
        self._over = over

    def __getattr__(self, name: str):
        over = object.__getattribute__(self, "_over")
        if name in over and over[name]:
            return over[name]
        return getattr(object.__getattribute__(self, "_s"), name)


def _tolerant_chain(chain: list[str], registry: dict[str, Builder], name: str, settings) -> list:
    """Build what can be built. A provider with a missing credential is DROPPED, not fatal.

    The chain exists so one provider being unusable is survivable; raising here would turn a single
    bad key into a total outage for the channel. Console is kept as the floor so the chain is never
    empty — an empty chain fails every send with no provider to name in the log.
    """
    built = []
    for driver in chain:
        builder = registry.get(driver)
        if builder is None:
            _log.warning("unknown_provider_skipped", extra={"channel": name, "provider": driver})
            continue
        try:
            built.append(builder(settings))
        except Exception:  # noqa: BLE001 — ConfigError for a missing key, anything a client raises
            _log.warning("provider_unconfigured_skipped",
                         extra={"channel": name, "provider": driver})
    if not built:
        built.append(registry["console"](settings))
        _log.warning("provider_chain_empty_fell_back_to_console", extra={"channel": name})
    return built


def make_provider_pool(settings, secret_store, runtime_config):
    """The pool the send path resolves through, so a credential change lands without a redeploy."""
    from app.core.provider_pool import ProviderPool

    return ProviderPool(
        settings, secret_store, runtime_config,
        build_email=lambda chain, creds: FailoverEmail(
            _tolerant_chain(chain, _EMAIL_PROVIDERS, "email", _Overlay(settings, creds))),
        build_sms=lambda chain, creds: FailoverSms(
            _tolerant_chain(chain, _SMS_PROVIDERS, "sms", _Overlay(settings, creds))),
        build_whatsapp=lambda chain, creds: FailoverWhatsApp(
            _tolerant_chain(chain, _WHATSAPP_PROVIDERS, "whatsapp", _Overlay(settings, creds))),
        # A provider bound to ONE specific number (a branch's), same token. A single-element failover:
        # it yields the same (ok, provider, message_id) shape the notifier expects, and single-element
        # means NO fallback — a send explicitly addressed to a number never goes out from another.
        build_whatsapp_identity=lambda token, phone_id: FailoverWhatsApp(
            [MetaWhatsApp(token, phone_id, settings.WHATSAPP_GRAPH_BASE)]),
        build_template_manager=lambda token, waba: _tenant_template_manager(settings, token, waba),
        build_flow_manager=lambda token, waba: _tenant_flow_manager(settings, token, waba),
        build_media_uploader=lambda token: _tenant_media_uploader(settings, token),
        ttl_s=settings.PROVIDER_REFRESH_SECONDS,
    )


def _tenant_template_manager(settings, token: str, waba: str):
    """A per-tenant Meta template manager, or None to fall back to the boot-built one.

    None when: the driver is console (no per-tenant identity), a credential is missing, or the
    resolved values ARE the configured ones (so a single-brand deployment reuses the boot manager
    unchanged, with nothing to migrate)."""
    if settings.WHATSAPP_TEMPLATE_MANAGER != "meta" or not token or not waba:
        return None
    if token == settings.WHATSAPP_ACCESS_TOKEN and waba == settings.WHATSAPP_BUSINESS_ACCOUNT_ID:
        return None
    return MetaTemplateManager(token, waba, settings.WHATSAPP_GRAPH_BASE)


def _tenant_flow_manager(settings, token: str, waba: str):
    """A per-tenant Meta Flow manager, or None to fall back to the boot-built one — the same rule as
    the per-tenant template manager: a Flow belongs to exactly one WABA, so a tenant with its own WABA
    provisions Flows under it, and a single-brand deployment (or the configured creds) reuses boot."""
    if settings.WHATSAPP_TEMPLATE_MANAGER != "meta" or not token or not waba:
        return None
    if token == settings.WHATSAPP_ACCESS_TOKEN and waba == settings.WHATSAPP_BUSINESS_ACCOUNT_ID:
        return None
    return MetaFlowManager(token, waba, settings.WHATSAPP_GRAPH_BASE)


def _tenant_media_uploader(settings, token: str):
    """A per-tenant Meta media uploader, or None to fall back to the boot one. The upload is App-scoped
    (META_APP_ID, one per deployment) but authenticates with the TENANT's token, so a tenant with its
    own token uploads under its own identity; the configured token reuses the boot uploader."""
    if settings.WHATSAPP_TEMPLATE_MANAGER != "meta" or not token or not settings.META_APP_ID:
        return None
    if token == settings.WHATSAPP_ACCESS_TOKEN:
        return None
    return MetaMediaUploader(token, settings.META_APP_ID, settings.WHATSAPP_GRAPH_BASE)


def _pick(registry: dict[str, Builder], driver: str, port_name: str, settings: Settings) -> Any:
    builder = registry.get(driver)
    if builder is None:
        allowed = ", ".join(sorted(registry))
        raise ConfigError(
            f"unknown {port_name} driver: {driver!r} — allowed: {allowed}"
        )
    return builder(settings)


@dataclass
class Container:
    datastore: DataStore
    cache: Cache
    event_bus: EventBus
    queue: Queue
    object_store: ObjectStore
    secrets: SecretsProvider
    token_verifier: TokenVerifier
    rate_limiter: RateLimiter
    telemetry: Telemetry
    audit_sink: Any = None
    consent_gate: Any = None
    email: Any = None
    sms: Any = None
    whatsapp: Any = None
    whatsapp_template_manager: Any = None
    whatsapp_flow_manager: Any = None
    whatsapp_media_uploader: Any = None
    renderer: Any = None

    def describe(self) -> list[dict[str, str]]:
        """[{port, driver}] for the service manifest."""
        rows = [
            {"port": "datastore", "driver": self.datastore.driver},
            {"port": "cache", "driver": self.cache.driver},
            {"port": "event_bus", "driver": self.event_bus.driver},
            {"port": "queue", "driver": self.queue.driver},
            {"port": "object_store", "driver": self.object_store.driver},
            {"port": "secrets", "driver": self.secrets.driver},
            {"port": "token_verifier", "driver": self.token_verifier.driver},
            {"port": "rate_limiter", "driver": self.rate_limiter.driver},
            {"port": "telemetry", "driver": self.telemetry.driver},
            *([{"port": "audit_sink", "driver": self.audit_sink.driver}]
              if self.audit_sink is not None else []),
        ]
        for name in ("email", "sms", "renderer"):
            adapter = getattr(self, name)
            if adapter is not None:
                rows.append({"port": name, "driver": adapter.driver})
        return rows

    def health_probes(self) -> dict[str, Any]:
        """name -> async health() for adapters that support it (readiness)."""
        probes: dict[str, Any] = {}
        for name in ("datastore", "cache", "event_bus", "queue", "object_store", "rate_limiter"):
            adapter = getattr(self, name)
            # Prefer health_state (a reason string: "ok" / "pool exhausted …" / "down") when the
            # adapter offers it, so readiness can name a FULL datastore distinctly from a DOWN one;
            # fall back to the boolean health() every adapter has.
            probe = getattr(adapter, "health_state", None) or getattr(adapter, "health", None)
            if probe is not None:
                probes[name] = probe
        return probes

    async def close(self) -> None:
        for name in ("datastore", "cache", "event_bus", "queue", "object_store", "rate_limiter"):
            adapter = getattr(self, name)
            close = getattr(adapter, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    pass


def _build_audit_sink(settings: Settings, event_bus) -> Any:
    """The eventbus sink must publish on the SAME bus the container hands out — a second instance
    would be a second in-memory broker, so events would vanish while the sink reported success."""
    driver = settings.AUDIT_SINK_DRIVER
    if driver == "eventbus":
        return EventBusAuditSink(event_bus, settings.AUDIT_TOPIC)
    return _pick(_AUDIT_SINK, driver, "audit_sink", settings)


def build_container(settings: Settings) -> Container:
    """Resolve every port from config. Fail-fast on any unknown driver."""
    from app.adapters import notify_http
    notify_http.configure(settings.HTTP_TIMEOUT_SECONDS)  # outbound timeout from config
    event_bus = _pick(_EVENTBUS, settings.EVENTBUS_DRIVER, "event_bus", settings)
    datastore = _pick(_DATASTORE, settings.DATASTORE_DRIVER, "datastore", settings)
    return Container(
        datastore=datastore,
        cache=_pick(_CACHE, settings.CACHE_DRIVER, "cache", settings),
        event_bus=event_bus,
        queue=_pick(_QUEUE, settings.QUEUE_DRIVER, "queue", settings),
        object_store=_pick(_OBJECTSTORE, settings.OBJECTSTORE_DRIVER, "object_store", settings),
        secrets=_pick(_SECRETS, settings.SECRETS_DRIVER, "secrets", settings),
        token_verifier=_pick(_TOKEN, settings.TOKEN_VERIFIER_DRIVER, "token_verifier", settings),
        rate_limiter=_pick(_RATELIMITER, settings.RATELIMITER_DRIVER, "rate_limiter", settings),
        telemetry=_pick(_TELEMETRY, settings.TELEMETRY_DRIVER, "telemetry", settings),
        audit_sink=_build_audit_sink(settings, event_bus),
        consent_gate=_pick(_CONSENT_GATE, settings.CONSENT_GATE_DRIVER, "consent_gate", settings),
        email=FailoverEmail(_build_chain(settings.email_chain, _EMAIL_PROVIDERS, "email", settings)),
        sms=FailoverSms(_build_chain(settings.sms_chain, _SMS_PROVIDERS, "sms", settings)),
        whatsapp=FailoverWhatsApp(
            _build_chain(settings.whatsapp_chain, _WHATSAPP_PROVIDERS, "whatsapp", settings)),
        whatsapp_template_manager=_build_template_manager(settings),
        whatsapp_flow_manager=_build_flow_manager(settings),
        whatsapp_media_uploader=_build_media_uploader(settings),
        renderer=SafeRenderer(),
    )


def _build_template_manager(settings):
    """The WhatsApp template lifecycle manager. `console` auto-approves (dev); `meta` reviews."""
    if settings.WHATSAPP_TEMPLATE_MANAGER == "meta":
        return MetaTemplateManager(settings.WHATSAPP_ACCESS_TOKEN,
                                   settings.WHATSAPP_BUSINESS_ACCOUNT_ID, settings.WHATSAPP_GRAPH_BASE)
    return ConsoleTemplateManager()


def _build_flow_manager(settings):
    """The WhatsApp Flow lifecycle manager (create/clone/publish). Same WABA + driver decision as the
    template manager — Flows and templates are two faces of one Business Account — so it follows
    WHATSAPP_TEMPLATE_MANAGER rather than introducing a second knob for the same choice."""
    if settings.WHATSAPP_TEMPLATE_MANAGER == "meta":
        return MetaFlowManager(settings.WHATSAPP_ACCESS_TOKEN,
                               settings.WHATSAPP_BUSINESS_ACCOUNT_ID, settings.WHATSAPP_GRAPH_BASE)
    return ConsoleFlowManager()


def _build_media_uploader(settings):
    """The WhatsApp media uploader (Resumable Upload → template media handle). Same driver decision as
    the template/flow managers; the upload is App-scoped (META_APP_ID) rather than WABA-scoped, but
    the credential + console-in-dev shape is the same, so it follows WHATSAPP_TEMPLATE_MANAGER."""
    if settings.WHATSAPP_TEMPLATE_MANAGER == "meta":
        return MetaMediaUploader(settings.WHATSAPP_ACCESS_TOKEN, settings.META_APP_ID,
                                 settings.WHATSAPP_GRAPH_BASE)
    return ConsoleMediaUploader()
