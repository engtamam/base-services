# =============================================================================
# identity-service — Configuration (fail-fast, ZERO-HARDCODED)
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
    SERVICE_NAME: str = "identity-service"
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
    # nosec B108 — /tmp is where the chart mounts a WRITABLE volume under readOnlyRootFilesystem;
    # this is the object store's data dir, not an insecure shared-temp file. Production uses s3.
    OBJECTSTORE_ROOT: str = "/tmp/objectstore"  # nosec B108
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

    # ── identity-service (Phase 6) — all config-driven, zero-hardcoded ──────
    PASSWORD_HASHER_DRIVER: str = "argon2"      # argon2 | bcrypt

    # How users identify themselves at login. `username` reproduces the historical
    # behavior exactly; `email` validates the address and folds case so one address is
    # one account. A new adapter (phone, a federated provider) is a new value here.
    IDENTITY_LOGIN_FIELD: str = "username"       # username | email

    # Salts the subject pseudonym written into every IMMUTABLE record (audit chain, ledgers,
    # invoices). FROZEN — see config/product.identifiers.lock.json. Change it and every pseudonym
    # re-maps: the history orphans and an erasure can no longer prove it erased the right person.
    PII_SALT: str = ""
    TOKEN_ISSUER_DRIVER: str = "null"           # null(deny) | hs256 | rs256 (identity SIGNS)
    TOKEN_SIGNING_SECRET: str = ""              # hs256 signing secret (>=32)
    TOKEN_PRIVATE_KEY: str = ""                 # rs256 PEM private key
    ACCESS_TTL_SECONDS: int = 900               # 15 min access token
    REFRESH_TTL_SECONDS: int = 604_800          # 7 day refresh token
    MFA_DRIVER: str = "null"                    # null (off) | totp
    MFA_ISSUER_NAME: str = "base-services"      # shown in authenticator apps
    MFA_TOTP_WINDOW: int = 1                     # TOTP clock-skew tolerance (± steps)
    REGISTRATION_ENABLED: bool = True           # open signup vs admin-only
    PASSWORD_MIN_LENGTH: int = 8                 # minimum password length policy
    # Per-account brute-force lockout (generic rate-limit doesn't stop targeted stuffing).
    LOGIN_MAX_ATTEMPTS: int = 5                  # consecutive failures before lockout
    LOGIN_ATTEMPT_WINDOW_SECONDS: int = 900      # window the failures are counted in
    LOGIN_LOCKOUT_SECONDS: int = 900            # how long the account stays locked

    # ── multi-tenant membership (Phase 31) ──────────────────────────────────
    # One person, many companies: a freelancer with two clients gets ONE login and a key to
    # each door — never one view of both. Default OFF: a single-tenant product behaves exactly
    # as it does today, byte for byte. This is not a migration anybody is forced into.
    IDENTITY_MULTI_TENANT: bool = False
    # Switching from a low-trust tenant to a high-trust one on an existing session widens the
    # blast radius if that session was stolen. On, a switch requires MFA re-proof. Default off
    # is a CHOICE (see phase-31 T10) — written down, not assumed.
    IDENTITY_TENANT_SWITCH_REAUTH: bool = False

    # Identity owns who works where. Without a directory here, every product service grows its own
    # copy of a person — and a second person to erase when they ask to be forgotten.
    #
    # The capability NAME is config: what a platform calls "may see the staff list" is a platform's
    # decision (`members:read`, `hr.view`, `admin`), not ours.
    IDENTITY_MEMBERS_READ_CAP: str = "members:read"
    IDENTITY_MEMBERS_PAGE_SIZE: int = 50         # default page
    IDENTITY_MEMBERS_MAX_PAGE_SIZE: int = 200    # a caller may not ask for the whole company at once

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
