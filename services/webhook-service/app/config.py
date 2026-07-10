# =============================================================================
# webhook-service — Configuration (fail-fast, ZERO-HARDCODED)
# -----------------------------------------------------------------------------
# Every knob is an env var validated by Pydantic at boot. If a required value is
# missing or invalid, the process refuses to start (fail-fast). Defaults are
# safe: they reach NO external system until explicitly pointed at real infra.
# No hardcoded hosts/URLs/secrets/limits anywhere in the codebase — they all
# live here, sourced from the environment.
# =============================================================================
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Values come from env vars (or a local .env file)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ── Identity ────────────────────────────────────────────────────────────
    SERVICE_NAME: str = "webhook-service"
    SERVICE_VERSION: str = "0.1.0"
    ENV: Literal["development", "production", "test"] = "production"

    # ── HTTP server ─────────────────────────────────────────────────────────
    HOST: str = "0.0.0.0"  # noqa: S104 — bind address is operator-controlled config, not hardcoded exposure
    PORT: int = 8000

    # ── Logging ─────────────────────────────────────────────────────────────
    LOG_LEVEL: Literal["debug", "info", "warning", "error"] = "info"

    # ── Request hardening ───────────────────────────────────────────────────
    REQUEST_TIMEOUT_MS: int = 30_000
    BODY_LIMIT_BYTES: int = 1_048_576  # 1 MiB

    # ── Security headers ────────────────────────────────────────────────────
    # CSP frame-ancestors — who may embed this service's responses in a frame.
    ALLOWED_FRAME_ANCESTORS: str = "'none'"

    # ── Internal service-to-service auth (REQUIRED) ─────────────────────────
    # Shared token for /internal/* endpoints. Must be strong (>= 32 chars).
    INTERNAL_SERVICE_TOKEN: str = Field(min_length=32)

    # ── Ports & adapters (Phase 2) — driver selection, ZERO-INFRA defaults ──
    # Every default reaches NO external system; point at real infra by config.
    DATASTORE_DRIVER: str = "memory"
    DATASTORE_DSN: str = ""  # e.g. postgresql://user:pass@host/db  (postgres driver)

    CACHE_DRIVER: str = "memory"
    CACHE_URL: str = ""  # e.g. redis://host:6379/0  (redis driver)

    EVENTBUS_DRIVER: str = "memory"
    EVENTBUS_URL: str = ""

    QUEUE_DRIVER: str = "memory"
    QUEUE_URL: str = ""

    OBJECTSTORE_DRIVER: str = "local"
    OBJECTSTORE_ROOT: str = "./.objectstore"  # local driver
    OBJECTSTORE_BUCKET: str = ""  # s3 driver
    OBJECTSTORE_ENDPOINT_URL: str = ""  # s3-compatible endpoint (config allowlist)

    SECRETS_DRIVER: str = "env"
    SECRETS_FILE: str = ""  # file driver: path to a KEY=VALUE file
    VAULT_ADDR: str = ""
    VAULT_TOKEN: str = ""
    VAULT_PATH: str = ""

    # Token verifier — 'null' denies all (deny-by-default) until a real driver +
    # key is configured. Prefer asymmetric (rs256/jwks): verify-only, never forge.
    TOKEN_VERIFIER_DRIVER: str = "null"
    TOKEN_ISSUER: str = ""
    TOKEN_AUDIENCE: str = ""
    TOKEN_HS256_SECRET: str = ""  # hs256 driver
    TOKEN_PUBLIC_KEY: str = ""  # rs256 driver (PEM public key)
    TOKEN_JWKS_URL: str = ""  # jwks driver (config allowlist)

    RATELIMITER_DRIVER: str = "memory"
    RATELIMIT_URL: str = ""
    RATELIMIT_MAX: int = 100
    RATELIMIT_WINDOW_SECONDS: int = 60

    TELEMETRY_DRIVER: str = "stdout"

    # ── CWE self-guard (Phase 3) ────────────────────────────────────────────
    CWE_GUARD_ENABLED: bool = True
    # SSRF allowlist (CWE-918): comma-separated hostnames egress may target.
    EGRESS_ALLOWED_HOSTS: str = ""
    # Path to a step->CWE map override (TOML). Empty => built-in default map.
    CWE_STEP_MAP_FILE: str = ""

    # ── webhook-service (Phase 18) — all config-driven, zero-hardcoded ────────
    WEBHOOK_TRANSPORT_DRIVER: str = "stdlib"   # stdlib | httpx | recording | null
    WEBHOOK_SIGNER_DRIVER: str = "hmac-sha256"  # hmac-sha256 | null

    # hot path
    SUBSCRIPTION_CACHE_TTL_SECONDS: int = 30

    # delivery + retry (full jitter — plain 2^n synchronises retries into a storm)
    DELIVERY_TIMEOUT_SECONDS: int = 10
    RETRY_BASE_SECONDS: int = 30
    RETRY_MAX_SECONDS: int = 21_600            # 6h ceiling
    RETRY_MAX_ATTEMPTS: int = 8                # then → dead-letter (never dropped)
    RETRY_JITTER: bool = True

    # circuit breaker (a dead endpoint must not eat the worker pool)
    CIRCUIT_FAILURE_THRESHOLD: int = 5
    CIRCUIT_RESET_SECONDS: int = 300

    # inbound (replay protection)
    INBOUND_TIMESTAMP_TOLERANCE_SECONDS: int = 300
    INBOUND_DEDUPE_TTL_SECONDS: int = 86_400
    INBOUND_SECRETS: str = ""                  # "stripe=whsec_a|whsec_old;github=s2" (| = rotation)

    # size caps (never DDoS a customer; never read a 1GB response)
    MAX_PAYLOAD_BYTES: int = 1_048_576         # 1 MiB
    MAX_RESPONSE_BYTES: int = 65_536           # 64 KiB (we only log a truncated body)
    ENDPOINT_CONCURRENCY: int = 4

    # SSRF guard (CWE-918) — customers register arbitrary URLs
    WEBHOOK_REQUIRE_HTTPS: bool = True
    WEBHOOK_ALLOWED_HOSTS: str = ""            # comma list; empty = any public host
    WEBHOOK_DENIED_HOSTS: str = ""
    WEBHOOK_ALLOW_PRIVATE_IPS: bool = False    # dev/test only — NEVER true in production

    WEBHOOK_LIST_LIMIT: int = 100

    @property
    def inbound_secrets(self) -> dict[str, list[str]]:
        """`source=secret|previous;source2=secret` — a source with no secret is never trusted."""
        out: dict[str, list[str]] = {}
        for pair in self.INBOUND_SECRETS.split(";"):
            source, _, secrets = pair.partition("=")
            source = source.strip()
            values = [s.strip() for s in secrets.split("|") if s.strip()]
            if source and values:
                out[source] = values
        return out

    @property
    def webhook_allowed_hosts(self) -> frozenset[str]:
        return frozenset(h.strip().lower() for h in self.WEBHOOK_ALLOWED_HOSTS.split(",") if h.strip())

    @property
    def webhook_denied_hosts(self) -> frozenset[str]:
        return frozenset(h.strip().lower() for h in self.WEBHOOK_DENIED_HOSTS.split(",") if h.strip())

    @property
    def egress_allowed_hosts(self) -> frozenset[str]:
        return frozenset(
            h.strip().lower() for h in self.EGRESS_ALLOWED_HOSTS.split(",") if h.strip()
        )

    # ── Derived helpers ─────────────────────────────────────────────────────
    @property
    def is_test(self) -> bool:
        return self.ENV == "test"

    @property
    def is_production(self) -> bool:
        return self.ENV == "production"

    @field_validator("INTERNAL_SERVICE_TOKEN")
    @classmethod
    def _reject_placeholder(cls, v: str) -> str:
        # Refuse obvious placeholder/dev tokens in any environment — fail fast so
        # a template default never ships as a real secret.
        lowered = v.strip().lower()
        if lowered.startswith("placeholder") or lowered in {"changeme", "change_me"}:
            raise ValueError("INTERNAL_SERVICE_TOKEN is a placeholder — set a real secret")
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the validated singleton settings.

    Raises pydantic.ValidationError at first call if the environment is invalid,
    which surfaces as a hard startup failure (fail-fast).
    """
    return Settings()  # type: ignore[call-arg]  # values are supplied by env
