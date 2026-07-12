# =============================================================================
# notification-service — Configuration (fail-fast, ZERO-HARDCODED)
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
    SERVICE_NAME: str = "notification-service"
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

    # ── notification-service (Phase 7) — channels via failover chains ───────
    # email providers: console | smtp | resend
    FEED_PAGE_SIZE: int = 100           # in-app feed items returned per request
    FEED_SCAN_LIMIT: int = 1000         # max rows scanned to build a user's feed
    NOTIFY_DEDUPE_TTL_SECONDS: int = 86_400   # idempotency window (retry won't re-send)
    HTTP_TIMEOUT_SECONDS: int = 15      # outbound provider HTTP/SMTP timeout
    EMAIL_CHAIN: str = "console"        # e.g. "resend,console" (try resend, fall back)
    # sms providers: console | twilio | unifonic (KSA) | taqnyat (KSA)
    SMS_CHAIN: str = "console"          # e.g. "unifonic,console"
    DEFAULT_LOCALE: str = "en"
    EMAIL_FROM: str = "no-reply@example.com"

    # SMTP (email 'smtp' provider) — stdlib
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_STARTTLS: bool = True

    # Resend (email) — POST https://api.resend.com/emails
    RESEND_API_KEY: str = ""
    # SendGrid (email) — POST https://api.sendgrid.com/v3/mail/send
    SENDGRID_API_KEY: str = ""
    # Mailgun (email) — POST {base}/v3/{domain}/messages  (EU: api.eu.mailgun.net)
    MAILGUN_API_KEY: str = ""
    MAILGUN_DOMAIN: str = ""
    MAILGUN_BASE: str = "https://api.mailgun.net"
    # Postmark (email) — POST https://api.postmarkapp.com/email
    POSTMARK_TOKEN: str = ""
    POSTMARK_STREAM: str = "outbound"

    # Twilio (sms, global) — POST .../Accounts/{SID}/Messages.json
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_FROM: str = ""
    # Vonage / Nexmo (sms, global) — POST https://rest.nexmo.com/sms/json
    VONAGE_API_KEY: str = ""
    VONAGE_API_SECRET: str = ""
    VONAGE_FROM: str = ""

    # Unifonic (sms, KSA) — POST https://el.cloud.unifonic.com/rest/SMS/messages
    UNIFONIC_APP_SID: str = ""
    UNIFONIC_SENDER: str = ""
    # Taqnyat (sms, KSA) — POST https://api.taqnyat.sa/v1/messages
    TAQNYAT_TOKEN: str = ""
    TAQNYAT_SENDER: str = ""
    # Msegat (sms, KSA) — POST https://www.msegat.com/gw/sendsms.php
    MSEGAT_USERNAME: str = ""
    MSEGAT_API_KEY: str = ""
    MSEGAT_SENDER: str = ""
    # Mobily.ws (sms, KSA) — POST https://api.mobily.ws/api/msgSend.php
    MOBILY_MOBILE: str = ""      # account number / username
    MOBILY_PASSWORD: str = ""
    MOBILY_SENDER: str = ""
    # Generic webhook (sms) — POST {to, message} JSON to any gateway you run
    SMS_WEBHOOK_URL: str = ""    # host must be in EGRESS_ALLOWED_HOSTS (SSRF)
    SMS_WEBHOOK_BEARER: str = ""

    # Salts the subject pseudonym written into every IMMUTABLE record (audit chain, ledgers,
    # invoices). FROZEN — see config/product.identifiers.lock.json. Change it and every pseudonym
    # re-maps: the history orphans and an erasure can no longer prove it erased the right person.
    PII_SALT: str = ""

    @property
    def email_chain(self) -> list[str]:
        return [c.strip() for c in self.EMAIL_CHAIN.split(",") if c.strip()]

    @property
    def sms_chain(self) -> list[str]:
        return [c.strip() for c in self.SMS_CHAIN.split(",") if c.strip()]

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
