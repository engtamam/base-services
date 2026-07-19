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


    # ── audit trail (Phase 38) — where privileged actions are witnessed ───────
    # `null` records NOTHING and says so; the boot guard refuses it in production, because a
    # service that cannot witness its own privileged actions has no audit trail at all — and an
    # empty hash chain looks like a working control while answering nothing.
    AUDIT_SINK_DRIVER: str = "null"          # null | http | eventbus
    AUDIT_SERVICE_URL: str = ""              # e.g. http://audit-service:8000
    # OUR ingest path is a default, not an assumption about another platform's endpoint.
    AUDIT_INGEST_PATH: str = "/internal/audit"
    AUDIT_TOPIC: str = "audit.events"        # eventbus sink
    # Short on purpose: this sits on the request path, and a slow audit must not become a slow API.
    AUDIT_TIMEOUT_SECONDS: int = 5
    # What happens when a CRITICAL action cannot be witnessed. Default closed: an action nobody can
    # prove is worse than a refused one for the actions investigations turn on. Routine actions
    # always fail open — the split is per action, declared by this service, not a global switch.
    AUDIT_FAIL_CLOSED: bool = True

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
    # Any S3-compatible store — AWS S3, MinIO, or a Saudi cloud's object storage. There is no
    # separate "minio driver": MinIO speaks S3, so it IS the s3 driver, pointed at your endpoint.
    #
    # Leave the keys EMPTY on AWS to use the ambient chain (IAM role / instance profile) — that is
    # strictly safer than static keys, so it stays the default. Set them for MinIO or anywhere the
    # ambient chain does not exist.
    OBJECTSTORE_ACCESS_KEY: str = ""
    OBJECTSTORE_SECRET_KEY: str = ""
    OBJECTSTORE_REGION: str = ""          # boto refuses to sign without one; MinIO ignores it
    # THE MINIO KNOB. Boto defaults to virtual-host addressing (https://bucket.host/key), which
    # needs a wildcard DNS record. MinIO and most on-prem stores serve path-style
    # (https://host/bucket/key) and simply fail to resolve otherwise.
    OBJECTSTORE_FORCE_PATH_STYLE: bool = False

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
    RATELIMIT_DEGRADE: str = "local"            # local | closed | open
    RATELIMIT_KEY_PARTS: str = "subject,ip"     # subject | ip | tenant | route (comma list)
    RATELIMIT_ROUTES: str = ""                  # JSON: [{"prefix","class","limit","window_s","cost"}]
    RATELIMIT_COUNT_STATUSES: str = ""          # e.g. "401" for a login surface; comma list

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
    # Which delivery outcomes an idempotency key remembers, so a retry with the same key returns the
    # existing delivery instead of creating a second. `queued` is included by default: a message
    # queued during a provider outage must dedupe a caller's retry, or the drain sends it twice.
    NOTIFY_DEDUPE_STATES: str = "sent,suppressed,queued"

    @property
    def notify_dedupe_states(self) -> tuple[str, ...]:
        return tuple(x.strip() for x in self.NOTIFY_DEDUPE_STATES.split(",") if x.strip())
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

    # ── delivery: an outage must DELAY a message, not destroy it ────────────
    # The send used to be inline: if the provider was down, the delivery was recorded "failed" and
    # that was the end of it. A five-minute outage silently destroyed every message sent during it.
    #
    #   queued  (default) a failed send that COULD succeed is re-queued with a jittered backoff and
    #           retried by the drain worker. An outage delays the message.
    #   inline  the old behaviour: one attempt, then "failed". Kept for a deployment that genuinely
    #           wants it — with eyes open, not by accident.
    NOTIFY_DELIVERY_MODE: str = "queued"        # queued | inline
    NOTIFY_MAX_ATTEMPTS: int = 5                # then DEAD_LETTER — findable, not silently lost
    NOTIFY_BACKOFF_BASE_MS: int = 1_000
    NOTIFY_BACKOFF_MAX_MS: int = 300_000        # 5 min ceiling; a doubling series runs away fast
    NOTIFY_DRAIN_LIMIT: int = 100               # deliveries re-attempted per tick

    # WHAT COUNTS AS RETRYABLE IS A PLATFORM DECISION. One provider says "throttled", another says
    # "rate_limited", a Saudi SMS gateway may say something we have never seen. Substring match,
    # case-insensitive, on the recorded error. PERMANENT wins over RETRYABLE: spending money to
    # re-send to a bad number is worse than giving up on one message a human can resend.
    NOTIFY_RETRYABLE_ERRORS: str = (
        "timeout,timederror,connection,unavailable,throttl,rate,502,503,504,send failed")
    NOTIFY_PERMANENT_ERRORS: str = (
        "invalid,malformed,not found,unknown channel,unsubscrib,blocked,suppress,400,401,403,404")
    # An error nobody classified. Retrying the unknown risks burning quota; giving up on it risks
    # losing a message. The default is to RETRY — the ceiling and the dead-letter bound the cost,
    # and a lost message has no bound at all.
    NOTIFY_UNKNOWN_ERROR_RETRYABLE: bool = True

    @property
    def notify_retryable(self) -> tuple[str, ...]:
        return tuple(x.strip() for x in self.NOTIFY_RETRYABLE_ERRORS.split(",") if x.strip())

    @property
    def notify_permanent(self) -> tuple[str, ...]:
        return tuple(x.strip() for x in self.NOTIFY_PERMANENT_ERRORS.split(",") if x.strip())

    # ── Derived helpers ─────────────────────────────────────────────────────
    @property
    def is_test(self) -> bool:
        return self.ENV == "test"

    @property
    def is_production(self) -> bool:
        return self.ENV == "production"

    @property
    def audit_ingest_url(self) -> str:
        """Where audit events go. Empty when unconfigured — never a guessed default, because a
        wrong host would send the audit trail somewhere the operator cannot read while every call
        still looked successful."""
        if not self.AUDIT_SERVICE_URL:
            return ""
        return self.AUDIT_SERVICE_URL.rstrip("/") + self.AUDIT_INGEST_PATH

    def assert_audit_config_ok(self) -> None:
        """Boot guard. Refuse to start production without somewhere to witness privileged actions.

        audit-service ran for months with a complete ingest endpoint and no callers: the chain
        verified perfectly and held nothing, and no test went red the entire time. A service that
        cannot record what it does must not quietly serve traffic as though it can.
        """
        if not self.is_production:
            return
        if self.AUDIT_SINK_DRIVER == "null":
            raise ValueError(
                "AUDIT_SINK_DRIVER=null in production: privileged actions would be performed and "
                "never recorded anywhere. Set it to 'http' (with AUDIT_SERVICE_URL) or 'eventbus'."
            )
        if self.AUDIT_SINK_DRIVER == "http" and not self.AUDIT_SERVICE_URL:
            raise ValueError(
                "AUDIT_SINK_DRIVER=http but AUDIT_SERVICE_URL is empty: every audit event would "
                "fail and the chain would stay empty while the service looked healthy."
            )
        if self.AUDIT_SERVICE_URL and not self.AUDIT_SERVICE_URL.startswith(("http://", "https://")):
            raise ValueError(
                f"AUDIT_SERVICE_URL {self.AUDIT_SERVICE_URL!r} must start with http:// or https://"
            )

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
