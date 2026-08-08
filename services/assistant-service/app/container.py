# =============================================================================
# assistant-service — Composition root (config-driven port -> adapter wiring)
# -----------------------------------------------------------------------------
# The registry is the ALLOWLIST: only listed drivers can be constructed, so an
# unknown/injected driver is impossible (fail-fast ConfigError at boot). Adapter
# selection is pure config (zero-hardcoded). The core depends on PORTS; only this
# file knows the concrete adapters.
# =============================================================================
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

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
from app.adapters.knowledge_source.catalog import CatalogKnowledgeSource
from app.adapters.knowledge_source.composite import CompositeKnowledgeSource
from app.adapters.knowledge_source.file import FileKnowledgeSource
from app.adapters.knowledge_source.memory import MemoryKnowledgeSource
from app.adapters.model.extractive import ExtractiveModel
from app.adapters.model.http_provider import HttpModel
from app.adapters.embeddings.http import HttpEmbeddings
from app.adapters.embeddings.null import NullEmbeddings
from app.core.conversations import ConversationStore, MessageCrypto
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
                                    timeout=s.AUDIT_TIMEOUT_SECONDS),
}
# Ask before processing personal data. `null` answers UNKNOWN and says so; the boot guard refuses
# it in production for any service that declares consent-gated processing, because a gate that is
# never asked is the same defect as a chain that is never written.
_CONSENT_GATE: dict[str, Builder] = {
    "null": lambda s: NullConsentGate(),
    "http": lambda s: HttpConsentGate(s.COMPLIANCE_SERVICE_URL, s.INTERNAL_SERVICE_TOKEN,
                                      timeout=s.CONSENT_TIMEOUT_SECONDS,
                                      path=s.CONSENT_CHECK_PATH),
}
_TELEMETRY: dict[str, Builder] = {
    "stdout": lambda s: StdoutTelemetry(),
}


# Where the assistant's facts come from. `memory` is the zero-infra default so the service boots
# and answers with nothing configured; an operator points it at a file, a catalog, or both.
def _one_kb_source(driver: str, settings: Settings) -> Any:
    if driver == "memory":
        return MemoryKnowledgeSource()
    if driver == "file":
        return FileKnowledgeSource(settings.KB_FILE_PATH)
    if driver == "catalog":
        return CatalogKnowledgeSource(settings.KB_CATALOG_URL, settings.INTERNAL_SERVICE_TOKEN,
                                      timeout=settings.KB_CATALOG_TIMEOUT_SECONDS,
                                      prefix=settings.KB_CATALOG_PREFIX)
    raise ConfigError(f"unknown knowledge_source driver: {driver!r} — "
                      f"allowed: memory, file, catalog, composite")


def _build_kb_source(settings: Settings) -> Any:
    driver = settings.KB_SOURCE_DRIVER
    if driver != "composite":
        return _one_kb_source(driver, settings)
    names = [n.strip() for n in settings.KB_COMPOSITE_SOURCES.split(",") if n.strip()]
    if not names:
        raise ConfigError("KB_SOURCE_DRIVER=composite but KB_COMPOSITE_SOURCES is empty — the "
                          "assistant would have no knowledge at all")
    # Order is priority: the FIRST source wins a duplicate id, so authored help placed first can
    # override one generated sentence without forking the generator.
    return CompositeKnowledgeSource([_one_kb_source(n, settings) for n in names])


def _build_conversations(settings: Settings, datastore) -> Any:
    """None when history is off. A service that keeps nothing has nothing to leak or erase, so the
    absence is the feature rather than a missing dependency."""
    if not settings.CONVERSATIONS_ENABLED:
        return None
    import datetime as _dt
    import uuid as _uuid

    crypto = MessageCrypto(settings.MESSAGE_KEY, required=settings.ENCRYPT_MESSAGES)
    # ISO-8601 UTC: it sorts correctly as a string, which is what the store's cursor ordering
    # relies on, and it needs no timezone table to compare.
    return ConversationStore(
        datastore, pii_salt=settings.PII_SALT, crypto=crypto,
        now=lambda: _dt.datetime.now(_dt.timezone.utc).isoformat(),
        new_id=lambda: _uuid.uuid4().hex)


_EMBEDDINGS: dict[str, Builder] = {
    "null": lambda s: NullEmbeddings(),
    "http": lambda s: HttpEmbeddings(s.EMBEDDINGS_URL, s.INTERNAL_SERVICE_TOKEN,
                                     model=s.EMBEDDINGS_MODEL,
                                     dimensions=s.EMBEDDINGS_DIMENSIONS,
                                     timeout=s.EMBEDDINGS_TIMEOUT_SECONDS),
}


_MODEL: dict[str, Builder] = {
    # Answers by quoting retrieved passages: no API key, no latency, and it cannot hallucinate.
    "extractive": lambda s: ExtractiveModel(max_passages=s.MODEL_EXTRACTIVE_MAX_PASSAGES),
    "http": lambda s: HttpModel(s.MODEL_URL, s.MODEL_API_KEY, model=s.MODEL_NAME,
                                shape=s.MODEL_SHAPE, timeout=s.MODEL_TIMEOUT_SECONDS,
                                temperature=s.MODEL_TEMPERATURE),
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
    kb_source: Any = None
    conversations: Any = None
    model: Any = None
    embeddings: Any = None
    consent_gate: Any = None

    def describe(self) -> list[dict[str, str]]:
        """[{port, driver}] for the service manifest."""
        return [
            {"port": "datastore", "driver": self.datastore.driver},
            {"port": "cache", "driver": self.cache.driver},
            {"port": "event_bus", "driver": self.event_bus.driver},
            {"port": "queue", "driver": self.queue.driver},
            {"port": "object_store", "driver": self.object_store.driver},
            {"port": "secrets", "driver": self.secrets.driver},
            {"port": "token_verifier", "driver": self.token_verifier.driver},
            {"port": "rate_limiter", "driver": self.rate_limiter.driver},
            {"port": "telemetry", "driver": self.telemetry.driver},
        ] + ([{"port": "audit_sink", "driver": self.audit_sink.driver}]
             if self.audit_sink is not None else [])           + ([{"port": "consent_gate", "driver": self.consent_gate.driver}]
             if self.consent_gate is not None else [])

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
    """The eventbus sink must publish on the SAME bus instance the container hands out — a second
    instance would be a second in-memory broker, so events would vanish in tests and in any
    single-process deployment while the sink still reported success."""
    driver = settings.AUDIT_SINK_DRIVER
    if driver == "eventbus":
        return EventBusAuditSink(event_bus, settings.AUDIT_TOPIC)
    return _pick(_AUDIT_SINK, driver, "audit_sink", settings)


def build_container(settings: Settings) -> Container:
    """Resolve every port from config. Fail-fast on any unknown driver."""
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
        kb_source=_build_kb_source(settings),
        conversations=_build_conversations(settings, datastore),
        model=_pick(_MODEL, settings.MODEL_DRIVER, "model", settings),
        embeddings=_pick(_EMBEDDINGS, settings.EMBEDDINGS_DRIVER, "embeddings", settings),
        consent_gate=_pick(_CONSENT_GATE, settings.CONSENT_GATE_DRIVER, "consent_gate", settings),
    )
