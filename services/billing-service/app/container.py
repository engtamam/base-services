# =============================================================================
# billing-service — Composition root (config-driven port -> adapter wiring)
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
    "postgres": lambda s: PostgresDataStore(s.DATASTORE_DSN),
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
                                    timeout=s.AUDIT_TIMEOUT_SECONDS),
}
_TELEMETRY: dict[str, Builder] = {
    "stdout": lambda s: StdoutTelemetry(),
}

# ── billing-service ─────────────────────────────────────────────────────────
from app.adapters.einvoicer.null import NullEInvoicer
from app.adapters.einvoicer.zatca_http import ZatcaHttpEInvoicer
from app.adapters.payment.aps import ApsGateway
from app.adapters.payment.checkout import CheckoutGateway
from app.adapters.payment.geidea import GeideaGateway
from app.adapters.payment.hyperpay import HyperPayGateway
from app.adapters.payment.moyasar import MoyasarGateway
from app.adapters.payment.null import NullGateway
from app.adapters.payment.paypal import PayPalGateway
from app.adapters.payment.recording import RecordingGateway
from app.adapters.payment.paytabs import PayTabsGateway
from app.adapters.payment.stripe import StripeGateway
from app.adapters.payment.tap import TapGateway
from app.adapters.pdf.null import NullPdf
from app.adapters.pdf.reportlab_renderer import ReportLabPdf
from app.adapters.tax.null import NullTax
from app.adapters.tax.vat import VatCalculator


class _UnconfiguredGateway:
    """A tenant was switched to a payment driver that is not configured (unknown name, or missing
    keys). Charging through the BOOT provider instead would put the money through the WRONG gateway,
    which is the one thing a per-tenant switch must never do. So every operation raises
    GatewayUnavailable — retryable by dunning — until the operator configures the chosen driver.
    """
    driver = "unconfigured"

    def __init__(self, name: str) -> None:
        self._name = name

    def _fail(self):
        from app.ports.payment_gateway import GatewayUnavailable
        raise GatewayUnavailable(
            f"gateway {self._name!r} is selected for this tenant but not configured — set its "
            f"credentials, or change PAYMENT_GATEWAY_DRIVER for the tenant")

    def charge(self, **_):
        self._fail()

    def refund(self, **_):
        self._fail()

    def verify_webhook(self, **_):
        from app.ports.payment_gateway import WebhookEvent
        return WebhookEvent(verified=False, type="unknown",
                            raw={"error": f"gateway {self._name!r} not configured"})


def rebuild_gateway(settings, resolved: dict, current, *, driver: str = ""):
    """The payment gateway for a tenant: the chosen DRIVER with operator-set credentials over env.

    `driver` is the tenant's runtime PAYMENT_GATEWAY_DRIVER (empty ⇒ the boot driver). Two failure
    directions, deliberately different:

      * SAME driver as boot, a bad/absent stored key ⇒ fall back to `current` (the boot gateway). A
        broken credential is an operator problem, not a total payment outage, and the boot gateway is
        a working answer for the provider this deployment actually runs.
      * SWITCHED to a different driver that will not build ⇒ FAIL CLOSED with _UnconfiguredGateway,
        NEVER `current`. Falling back to the boot provider would charge the wrong gateway — exactly
        the mistake a per-tenant switch exists to make impossible.
    """
    from app.core.credentials import Overlay

    driver = driver or settings.PAYMENT_GATEWAY_DRIVER
    switched = driver != settings.PAYMENT_GATEWAY_DRIVER

    # Nothing to change: same driver AND no stored credential differs from the configured one. Handing
    # back a fresh object would discard whatever the boot gateway holds between calls for no benefit.
    creds_same = all(v == (getattr(settings, k, "") or "") for k, v in resolved.items())
    if not switched and creds_same:
        return current

    builder = _PAYMENT.get(driver)
    if builder is None:
        # Unknown driver name. A switch to it fails closed; the boot driver being unknown is a boot
        # bug, so keep `current` there.
        return _UnconfiguredGateway(driver) if switched else current
    try:
        return builder(Overlay(settings, resolved))
    except Exception:  # noqa: BLE001 — GatewayUnavailable for a missing key, anything a client raises
        _log.warning("gateway_rebuild_failed", extra={"driver": driver, "switched": switched})
        return _UnconfiguredGateway(driver) if switched else current


def _pay(cls):
    return lambda s: cls(api_key=s.PAYMENT_API_KEY, base=s.PAYMENT_API_BASE,
                         extra_id=s.PAYMENT_EXTRA_ID, extra_secret=s.PAYMENT_EXTRA_SECRET,
                         webhook_secret=s.PAYMENT_WEBHOOK_SECRET)


def build_wallet(settings, token_provider):
    """The wallet the grant outbox delivers through.

    Falls back to the null driver on a configuration error rather than refusing to boot: a bad
    wallet URL must not stop the service from taking payments. The grants still accumulate and are
    delivered once the configuration is corrected, which is the whole reason they are written down
    before they are sent.
    """
    from app.adapters.wallet.null import NullWallet

    if settings.WALLET_DRIVER != "http":
        return NullWallet()
    from app.adapters.wallet.http import HttpWallet
    try:
        return HttpWallet(settings.WALLET_SERVICE_URL, token_provider,
                          allowed_hosts=settings.egress_allowed_hosts,
                          timeout_seconds=settings.WALLET_TIMEOUT_SECONDS)
    except ValueError as exc:
        _log.error("wallet_driver_misconfigured_using_null", extra={"error": str(exc)})
        return NullWallet()


_PAYMENT: dict[str, Builder] = {
    "null": lambda s: NullGateway(),
    "recording": lambda s: RecordingGateway(),
    "stripe": _pay(StripeGateway), "moyasar": _pay(MoyasarGateway), "tap": _pay(TapGateway),
    "hyperpay": _pay(HyperPayGateway), "paytabs": _pay(PayTabsGateway),
    "geidea": _pay(GeideaGateway), "aps": _pay(ApsGateway),
    "paypal": _pay(PayPalGateway), "checkout": _pay(CheckoutGateway),
}
_TAX: dict[str, Builder] = {
    "vat": lambda s: VatCalculator(s.VAT_RATE),
    "null": lambda s: NullTax(),
}
_EINVOICER: dict[str, Builder] = {
    "null": lambda s: NullEInvoicer(),
    "zatca-http": lambda s: ZatcaHttpEInvoicer(s.ZATCA_SERVICE_URL, s.ZATCA_SERVICE_TOKEN),
}
_PDF: dict[str, Builder] = {
    "null": lambda s: NullPdf(),
    "reportlab": lambda s: ReportLabPdf(
        font_path=s.INVOICE_FONT_PATH, max_lines=s.INVOICE_MAX_LINES,
        brand={"name": s.INVOICE_SELLER_NAME,
               "seller": {"name": s.INVOICE_SELLER_NAME, "vat_number": s.INVOICE_SELLER_VAT,
                          "cr_number": s.INVOICE_SELLER_CR,
                          "address": s.INVOICE_SELLER_ADDRESS}}),
}


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
    payment_gateway: Any = None
    tax_calculator: Any = None
    einvoicer: Any = None
    pdf_renderer: Any = None

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
        for name in ("payment_gateway", "tax_calculator", "einvoicer", "pdf_renderer"):
            adapter = getattr(self, name)
            if adapter is not None:
                rows.append({"port": name, "driver": adapter.driver})
        return rows

    def health_probes(self) -> dict[str, Any]:
        """name -> async health() for adapters that support it (readiness)."""
        probes: dict[str, Any] = {}
        for name in ("datastore", "cache", "event_bus", "queue", "object_store", "rate_limiter"):
            adapter = getattr(self, name)
            if hasattr(adapter, "health"):
                probes[name] = adapter.health
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
    event_bus = _pick(_EVENTBUS, settings.EVENTBUS_DRIVER, "event_bus", settings)
    return Container(
        datastore=_pick(_DATASTORE, settings.DATASTORE_DRIVER, "datastore", settings),
        cache=_pick(_CACHE, settings.CACHE_DRIVER, "cache", settings),
        event_bus=event_bus,
        queue=_pick(_QUEUE, settings.QUEUE_DRIVER, "queue", settings),
        object_store=_pick(_OBJECTSTORE, settings.OBJECTSTORE_DRIVER, "object_store", settings),
        secrets=_pick(_SECRETS, settings.SECRETS_DRIVER, "secrets", settings),
        token_verifier=_pick(_TOKEN, settings.TOKEN_VERIFIER_DRIVER, "token_verifier", settings),
        rate_limiter=_pick(_RATELIMITER, settings.RATELIMITER_DRIVER, "rate_limiter", settings),
        telemetry=_pick(_TELEMETRY, settings.TELEMETRY_DRIVER, "telemetry", settings),
        audit_sink=_build_audit_sink(settings, event_bus),
        payment_gateway=_pick(_PAYMENT, settings.PAYMENT_GATEWAY_DRIVER, "payment_gateway", settings),
        tax_calculator=_pick(_TAX, settings.TAX_CALCULATOR_DRIVER, "tax_calculator", settings),
        einvoicer=_pick(_EINVOICER, settings.EINVOICER_DRIVER, "einvoicer", settings),
        pdf_renderer=_pick(_PDF, settings.PDF_RENDERER_DRIVER, "pdf_renderer", settings),
    )
