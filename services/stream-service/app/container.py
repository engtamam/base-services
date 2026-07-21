# =============================================================================
# stream-service — Composition root (config-driven port -> adapter wiring)
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
# Clip export. `null` produces nothing and says so — never a fake ready, never a stuck pending.
_CLIP_EXPORTER: dict[str, Builder] = {
    "null": lambda s: NullClipExporter(),
    "ffmpeg": lambda s: FfmpegClipExporter(
        binary=s.EXPORT_FFMPEG_BIN, timeout_seconds=s.EXPORT_TIMEOUT_SECONDS,
        args_by_format=s.export_args, content_types=s.export_content_types,
        protocols=s.EXPORT_PROTOCOLS),
}
_TELEMETRY: dict[str, Builder] = {
    "stdout": lambda s: StdoutTelemetry(),
}

# ── stream-service ports (Phase 35) ──────────────────────────────────────────
from app.adapters.clip_exporter.ffmpeg import FfmpegClipExporter
from app.adapters.clip_exporter.null import NullClipExporter
from app.adapters.stream.local_placer import LocalSessionPlacer
from app.adapters.stream.null_adapters import (
    DirectMediaEgress, NoneDiscovery, NoneMotionDetector, NullPtzController, TurnMediaEgress,
)
from app.adapters.stream.null_engine import NullStreamEngine
from app.adapters.stream.recording_sink import (
    LocalRecordingSink, MemoryRecordingSink, S3RecordingSink,
)
from app.adapters.stream.signed_playback import HmacSignedPlayback
from app.core.sealing import Sealer

from app.adapters.stream.ffmpeg_engine import FfmpegStreamEngine

_STREAM_ENGINE: dict[str, Builder] = {
    "null": lambda s: NullStreamEngine(),
    "ffmpeg": lambda s: FfmpegStreamEngine(s.FFMPEG_PATH),
}
from app.adapters.stream.onvif_discovery import OnvifDiscovery
from app.adapters.stream.onvif_ptz import OnvifPtzController

_PTZ: dict[str, Builder] = {
    "null": lambda s: NullPtzController(),
    "onvif": lambda s: OnvifPtzController(move_timeout_seconds=s.PTZ_MOVE_TIMEOUT_SECONDS),
}
from app.adapters.stream.ffmpeg_motion import FfmpegMotionDetector

_MOTION: dict[str, Builder] = {
    "none": lambda s: NoneMotionDetector(),
    "ffmpeg-scene": lambda s: FfmpegMotionDetector(s.FFMPEG_PATH),
}
_DISCOVERY: dict[str, Builder] = {
    "none": lambda s: NoneDiscovery(),
    "onvif-ws": lambda s: OnvifDiscovery(),
}
_MEDIA_EGRESS: dict[str, Builder] = {
    "direct": lambda s: DirectMediaEgress(),
    "turn": lambda s: TurnMediaEgress(s.turn_urls, s.TURN_SECRET),
}
_SIGNED_PLAYBACK: dict[str, Builder] = {
    "hmac": lambda s: HmacSignedPlayback(_playback_secret(s)),
}


def _recording_sink(s: Settings, secret_store=None, resolvers=None):
    driver = s.RECORDING_SINK_DRIVER
    if driver == "memory":
        return MemoryRecordingSink()
    if driver == "local":
        return LocalRecordingSink(s.OBJECTSTORE_ROOT + "/recordings")
    if driver == "s3":
        # Write segments straight to the object store (durable, no cross-service auth).
        return S3RecordingSink(_pick(_OBJECTSTORE, "s3", "object_store", s))
    if driver == "file-service":
        # Post segments to file-service (scanned, content-addressed, presign-served storage). Until a
        # URL is set, dev/test uses a local dir so the service boots with zero infra; production
        # requires the URL (assert_stream_config_ok).
        if not s.RECORDING_FILE_SERVICE_URL:
            return LocalRecordingSink(s.OBJECTSTORE_ROOT + "/recordings")
        from app.adapters.stream.file_service_sink import FileServiceRecordingSink
        # The upload token is resolved per use so an operator can rotate it from the panel; the
        # configured value stays the fallback, so nothing changes for a deployment with no KEK.
        from app.core.operator_credentials import CredentialResolver
        resolver = CredentialResolver(
            secret_store, "RECORDING_FILE_SERVICE_TOKEN", s.RECORDING_FILE_SERVICE_TOKEN,
            ttl_s=s.RECORDING_CREDENTIAL_REFRESH_SECONDS)
        if resolvers is not None:
            resolvers.append(resolver)
        return FileServiceRecordingSink(s.RECORDING_FILE_SERVICE_URL, s.RECORDING_FILE_SERVICE_TOKEN,
                                        token_resolver=resolver)
    raise ConfigError(f"unknown RECORDING_SINK_DRIVER: {driver!r}")


def _seal_key(s: Settings) -> str:
    # SEAL_KEY is required in production (boot guard). In dev/test it falls back to the internal
    # token so the service still seals credentials with a real secret and boots with zero config.
    return s.SEAL_KEY or s.INTERNAL_SERVICE_TOKEN


def _playback_secret(s: Settings) -> str:
    return s.PLAYBACK_SIGNING_SECRET or s.INTERNAL_SERVICE_TOKEN


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
    # stream ports
    stream_engine: Any
    session_placer: Any
    ptz: Any
    motion: Any
    discovery: Any
    recording_sink: Any
    signed_playback: Any
    media_egress: Any
    sealer: Any
    audit_sink: Any = None
    # Built here because the store needs the datastore, and the sink needs the store. None when no
    # KEK is configured: the operator surface then refuses rather than storing in the clear.
    secret_store: Any = None
    credential_resolvers: tuple = ()
    clip_exporter: Any = None

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
            *([{"port": "audit_sink", "driver": self.audit_sink.driver}]
              if self.audit_sink is not None else []),
            {"port": "stream_engine", "driver": self.stream_engine.driver},
            {"port": "session_placer", "driver": self.session_placer.driver},
            {"port": "ptz", "driver": self.ptz.driver},
            {"port": "motion", "driver": self.motion.driver},
            {"port": "discovery", "driver": self.discovery.driver},
            {"port": "recording_sink", "driver": self.recording_sink.driver},
            {"port": "signed_playback", "driver": self.signed_playback.driver},
            {"port": "media_egress", "driver": self.media_egress.driver},
            {"port": "clip_exporter", "driver": self.clip_exporter.driver},
        ]

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
    engine = _pick(_STREAM_ENGINE, settings.STREAM_ENGINE_DRIVER, "stream_engine", settings)
    # The placer is built FROM the engine (local runs it in-process); worker-pool (35.4-later) will
    # take a dispatcher instead, same interface.
    if settings.SESSION_PLACER_DRIVER == "local":
        placer = LocalSessionPlacer(engine)
    else:
        raise ConfigError(f"session placer {settings.SESSION_PLACER_DRIVER!r} is built later")
    event_bus = _pick(_EVENTBUS, settings.EVENTBUS_DRIVER, "event_bus", settings)
    datastore = _pick(_DATASTORE, settings.DATASTORE_DRIVER, "datastore", settings)
    secret_store = None
    if settings.SECRETS_KEK:
        from app.core.secret_store import SecretStore
        secret_store = SecretStore(datastore, settings.SECRETS_KEK,
                                   rotation_window_s=settings.SECRETS_ROTATION_WINDOW_SECONDS)
    resolvers: list = []
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
        stream_engine=engine,
        session_placer=placer,
        ptz=_pick(_PTZ, settings.PTZ_DRIVER, "ptz", settings),
        motion=_pick(_MOTION, settings.MOTION_DRIVER, "motion", settings),
        discovery=_pick(_DISCOVERY, settings.DISCOVERY_DRIVER, "discovery", settings),
        recording_sink=_recording_sink(settings, secret_store, resolvers),
        media_egress=_pick(_MEDIA_EGRESS, settings.MEDIA_EGRESS_DRIVER, "media_egress", settings),
        clip_exporter=_pick(_CLIP_EXPORTER, settings.EXPORT_DRIVER, "clip_exporter", settings),
        signed_playback=_pick(_SIGNED_PLAYBACK, settings.SIGNED_PLAYBACK_DRIVER, "signed_playback", settings),
        sealer=Sealer(_seal_key(settings)),
        secret_store=secret_store,
        credential_resolvers=tuple(resolvers),
    )
