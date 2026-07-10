# =============================================================================
# zatca-service — Configuration (fail-fast, ZERO-HARDCODED)
# -----------------------------------------------------------------------------
# Every knob is an env var validated by Pydantic at boot. If a required value is
# missing or invalid, the process refuses to start (fail-fast). Defaults are
# safe: they reach NO external system until explicitly pointed at real infra.
# No hardcoded hosts/URLs/secrets/limits anywhere in the codebase — they all
# live here, sourced from the environment.
# =============================================================================
from __future__ import annotations

from functools import lru_cache
from typing import ClassVar, Literal

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
    SERVICE_NAME: str = "zatca-service"
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
    OBJECTSTORE_ROOT: str = "/tmp/objectstore"  # local driver; under the chart's
    #                                            writable /tmp mount, not the read-only
    #                                            root. production uses s3 + a bucket.
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

    # ── zatca-service (Phase 13) — all config-driven, zero-hardcoded ────────
    ZATCA_SIGNER_DRIVER: str = "null"        # null(fail-closed) | sdk-sidecar
    ZATCA_SIGNER_URL: str = ""               # official-SDK Java sidecar base URL
    ZATCA_SIGNER_TOKEN: str = ""             # X-Internal-Token for the sidecar
    FATOORA_CLIENT_DRIVER: str = "null"      # null | sidecar | direct
    # WHICH ZATCA is this. Validated (a typo like "prod" must not silently mean "not
    # production") and enforced: declaring `production` while pointing at nothing, or at a
    # fail-closed client, is a service that believes it is clearing invoices and is not.
    FATOORA_ENV: str = "sandbox"             # sandbox | simulation | production
    FATOORA_BASE_URL: str = ""               # env base URL (host must be egress-allowlisted)
    QR_RENDERER_DRIVER: str = "null"         # null | segno
    QR_FORMAT: str = "png"                    # png | svg
    ZATCA_VALIDATE_BEFORE_SUBMIT: bool = False  # optional official-SDK validate pass before submit
    VAT_RATE: str = "0.15"
    CURRENCY: str = "SAR"
    ZATCA_RETRY_BASE_MS: int = 60_000        # 60s
    ZATCA_RETRY_MAX_MS: int = 21_600_000     # 6h
    ZATCA_RETRY_MAX_ATTEMPTS: int = 12
    ZATCA_SEAL_KEY: str = ""                 # AES-GCM key for CSID at rest (falls back to INTERNAL token)
    ZATCA_LIST_LIMIT: int = 100

    @property
    def seal_key(self) -> str:
        return self.ZATCA_SEAL_KEY or self.INTERNAL_SERVICE_TOKEN

    @property
    def egress_allowed_hosts(self) -> frozenset[str]:
        return frozenset(
            h.strip().lower() for h in self.EGRESS_ALLOWED_HOSTS.split(",") if h.strip()
        )

    # ── Derived helpers ─────────────────────────────────────────────────────
    FATOORA_ENVIRONMENTS: ClassVar[tuple[str, ...]] = ("sandbox", "simulation", "production")

    @property
    def is_test(self) -> bool:
        return self.ENV == "test"

    @property
    def is_production(self) -> bool:
        return self.ENV == "production"

    @property
    def is_fatoora_production(self) -> bool:
        """Are we clearing invoices against the REAL ZATCA?

        Consumed by the composition root, which refuses to start a `production` service that
        cannot actually reach ZATCA, and by the manifest, so an operator can see at a glance
        which Fatoora a running pod is talking to.
        """
        return self.FATOORA_ENV == "production"

    @field_validator("FATOORA_ENV")
    @classmethod
    def _known_fatoora_env(cls, v: str) -> str:
        # A typo ("prod", "prd") must never quietly resolve to "not production": it would
        # silently downgrade a live tax integration into a sandbox one.
        value = v.strip().lower()
        if value not in cls.FATOORA_ENVIRONMENTS:
            raise ValueError(
                f"FATOORA_ENV must be one of {', '.join(cls.FATOORA_ENVIRONMENTS)} — got {v!r}"
            )
        return value

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
