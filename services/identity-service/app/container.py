# =============================================================================
# identity-service — Composition root (config-driven port -> adapter wiring)
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
from app.adapters.notifier.http import HttpNotifier
from app.adapters.notifier.null import NullNotifier
from app.adapters.telemetry.stdout import StdoutTelemetry
from app.adapters.token_verifier.hs256 import HS256Verifier
from app.adapters.token_verifier.jwks import JWKSVerifier
from app.adapters.token_verifier.null import NullVerifier
from app.adapters.token_verifier.rs256 import RS256Verifier
from app.config import Settings
from app.ports.cache import Cache
from app.ports.datastore import DataStore
from app.ports.errors import ConfigError
from app.adapters.login_identifier import EmailIdentifier, UsernameIdentifier
from app.ports.login_identifier import LoginIdentifier
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
# How a message reaches a human. `null` sends nothing and REPORTS that it sent nothing — never a
# fake success, because "verification mail sent" with nothing delivered is the failure that looks
# like success and locks people out.
_NOTIFIER: dict[str, Builder] = {
    "null": lambda s: NullNotifier(),
    "http": lambda s: HttpNotifier(s.NOTIFY_SERVICE_URL, s.INTERNAL_SERVICE_TOKEN,
                                   timeout=s.NOTIFY_TIMEOUT_SECONDS, path=s.NOTIFY_PATH),
}
_TELEMETRY: dict[str, Builder] = {
    "stdout": lambda s: StdoutTelemetry(),
}

# ── identity-service registries ─────────────────────────────────────────────
from app.adapters.caps_resolver.authz import AuthzCaps
from app.adapters.caps_resolver.membership import MembershipCaps
from app.adapters.mfa.null import NullMfa
from app.adapters.mfa.totp import TotpMfa
from app.adapters.password_hasher.argon2 import Argon2Hasher
from app.adapters.password_hasher.bcrypt import BcryptHasher
from app.adapters.token_issuer.hs256 import HS256Issuer
from app.adapters.token_issuer.null import NullIssuer
from app.adapters.token_issuer.rs256 import RS256Issuer

_PASSWORD_HASHER: dict[str, Builder] = {
    "argon2": lambda s: Argon2Hasher(),
    "bcrypt": lambda s: BcryptHasher(),
}
_TOKEN_ISSUER: dict[str, Builder] = {
    "null": lambda s: NullIssuer(),
    "hs256": lambda s: HS256Issuer(s.TOKEN_SIGNING_SECRET, s.TOKEN_ISSUER, s.TOKEN_AUDIENCE),
    "rs256": lambda s: RS256Issuer(s.TOKEN_PRIVATE_KEY, s.TOKEN_ISSUER, s.TOKEN_AUDIENCE),
}
_MFA: dict[str, Builder] = {
    "null": lambda s: NullMfa(),
    "totp": lambda s: TotpMfa(s.MFA_ISSUER_NAME, s.MFA_TOTP_WINDOW),
}
_LOGIN_IDENTIFIER: dict[str, Builder] = {
    "username": lambda s: UsernameIdentifier(),
    "email": lambda s: EmailIdentifier(),
}
_CAPS_RESOLVER: dict[str, Builder] = {
    # Roles are the input; capabilities are derived. `membership` keeps them on the row (no authz
    # needed, but a role means whatever it meant the day you were hired). `authz` derives them, so
    # editing a role changes what everyone holding it may do, on their next mint.
    "membership": lambda s: MembershipCaps(),
    "authz": lambda s: AuthzCaps(
        s.AUTHZ_URL, s.AUTHZ_INTERNAL_TOKEN,
        timeout_ms=s.AUTHZ_TIMEOUT_MS, ttl_seconds=s.AUTHZ_CAPS_TTL_SECONDS,
        path=s.AUTHZ_EFFECTIVE_PATH,
        token_header=s.AUTHZ_TOKEN_HEADER, caps_field=s.AUTHZ_CAPS_FIELD,
        subject_param=s.AUTHZ_SUBJECT_PARAM, tenant_param=s.AUTHZ_TENANT_PARAM,
        roles_param=s.AUTHZ_ROLES_PARAM, roles_sep=s.AUTHZ_ROLES_SEPARATOR),
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
    notifier: Any = None
    password_hasher: Any = None
    token_issuer: Any = None
    mfa: Any = None
    login_identifier: LoginIdentifier = None  # type: ignore[assignment]
    caps_resolver: Any = None

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
        for name in ("password_hasher", "token_issuer", "mfa", "login_identifier"):
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
        notifier=_pick(_NOTIFIER, settings.NOTIFIER_DRIVER, "notifier", settings),
        password_hasher=_pick(_PASSWORD_HASHER, settings.PASSWORD_HASHER_DRIVER, "password_hasher", settings),
        token_issuer=_pick(_TOKEN_ISSUER, settings.TOKEN_ISSUER_DRIVER, "token_issuer", settings),
        mfa=_pick(_MFA, settings.MFA_DRIVER, "mfa", settings),
        caps_resolver=_pick(_CAPS_RESOLVER, settings.IDENTITY_CAPS_SOURCE, "caps_resolver", settings),
        login_identifier=_pick(_LOGIN_IDENTIFIER, settings.IDENTITY_LOGIN_FIELD,
                               "login_identifier", settings),
    )
