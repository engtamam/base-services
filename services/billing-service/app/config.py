# =============================================================================
# billing-service — Configuration (fail-fast, ZERO-HARDCODED)
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
    SERVICE_NAME: str = "billing-service"
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

    # ── billing-service (Phase 14) — all config-driven, zero-hardcoded ─────
    PAYMENT_GATEWAY_DRIVER: str = "null"     # null|stripe|moyasar|tap|hyperpay|paytabs|paypal|checkout
    PAYMENT_API_KEY: str = ""                # gateway secret/API key (Bearer/Basic)
    PAYMENT_API_BASE: str = ""              # override gateway base URL (host egress-allowlisted)
    PAYMENT_EXTRA_ID: str = ""               # HyperPay entityId / PayTabs profile_id / APS merchant_identifier
    PAYMENT_EXTRA_SECRET: str = ""           # Geidea api-password / APS SHA request phrase
    PAYMENT_WEBHOOK_SECRET: str = ""         # webhook signature secret (APS: SHA response phrase)
    TAX_CALCULATOR_DRIVER: str = "vat"       # vat | null
    VAT_RATE: str = "0.15"                    # KSA standard rate
    REVERSE_CHARGE_B2B: bool = False          # B2B w/ buyer VAT → 0-rate (cross-border reverse charge)
    EINVOICER_DRIVER: str = "null"           # null | zatca-http
    ZATCA_SERVICE_URL: str = ""              # base URL of zatca-service (/internal/zatca/finalize)
    ZATCA_SERVICE_TOKEN: str = ""            # X-Internal-Token for zatca-service
    PDF_RENDERER_DRIVER: str = "null"        # null | reportlab
    CURRENCY: str = "SAR"
    TRIAL_DAYS: int = 14
    GRANDFATHER: bool = True                  # existing subs keep signup price/limits until migrated
    DUNNING_RETRY_BASE_HOURS: int = 24
    DUNNING_MAX_ATTEMPTS: int = 4
    GRACE_DAYS: int = 7
    BILLING_LIST_LIMIT: int = 100

    # ── usage metering / entitlement enforcement (Phase 25) ──────────────────
    # Limits are cacheable (a plan changes on upgrade, not per request). USAGE IS NOT:
    # cache a counter and two pods both approve the 26th seat. Only this TTL exists.
    LIMITS_CACHE_TTL_SECONDS: int = 300
    # Period used when a metered entitlement does not name one.
    USAGE_DEFAULT_PERIOD: str = "billing_period"   # day | week | month | billing_period
    # How long a consume() idempotency record is honoured (at-least-once redelivery window).
    USAGE_IDEMPOTENCY_TTL_SECONDS: int = 86_400
    # What an entitlement key absent from the plan resolves to. 0 = deny. Deny-by-default:
    # a typo'd key must never unlock a paid feature.
    ENTITLEMENT_UNKNOWN_KEY_DEFAULT: int = 0

    # ── off-gateway settlement (Phase 27) ────────────────────────────────────
    # `name:issuer-policy`. NO method is ever named in code — this is the allowlist, and an
    # unknown method fails closed. `issuer` is the institution that minted the reference (a
    # bank, a clearing house, a terminal): a reference is unique per ISSUER, not per planet,
    # so two banks may legitimately mint the same string. Cash has no issuer.
    SETTLEMENT_METHODS: str = "bank_transfer:issuer,cheque:issuer,offline_card:issuer,cash:no-issuer"
    # May an invoice be settled in tranches? A 60-day enterprise often pays in two.
    SETTLEMENT_ALLOW_PARTIAL: bool = True
    # A correspondent bank takes its fee out of the wire: a 100 000.00 invoice arrives as
    # 99 980.00. Without a tolerance every enterprise invoice sits `open` over 20 halalas.
    SETTLEMENT_TOLERANCE_MINOR: int = 0
    # `paid_at` is the bank's VALUE DATE, so it is in the past. Unbounded backdating rewrites
    # a closed tax period.
    SETTLEMENT_MAX_BACKDATE_DAYS: int = 90

    # The ledger balance is advanced under compare-and-swap. Losers retry with FULL JITTER;
    # without the backoff, 50 concurrent charges on one tenant exhausted the retries against a
    # real PostgreSQL and raised LEDGER_CONTENTION. Defaults set from that measurement.
    LEDGER_CAS_MAX_RETRIES: int = 20
    LEDGER_CAS_BACKOFF_MS: int = 12           # base for the full-jitter backoff; 0 = no sleep

    @property
    def settlement_methods(self) -> dict[str, bool]:
        """{method: requires_issuer}. Fail-fast on nonsense — never a silent default."""
        from app.ports.errors import ConfigError  # noqa: PLC0415 — avoid an import cycle

        out: dict[str, bool] = {}
        for chunk in self.SETTLEMENT_METHODS.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            parts = chunk.split(":")
            if len(parts) != 2:
                raise ConfigError(
                    f"SETTLEMENT_METHODS entry must be name:issuer-policy — got {chunk!r}")
            name, policy = (p.strip() for p in parts)
            if not name:
                raise ConfigError("SETTLEMENT_METHODS: empty method name")
            if policy not in ("issuer", "no-issuer"):
                raise ConfigError(
                    f"SETTLEMENT_METHODS: policy must be issuer|no-issuer — got {policy!r}")
            if name in out:
                raise ConfigError(f"SETTLEMENT_METHODS: duplicate method {name!r}")
            out[name] = policy == "issuer"
        if not out:
            raise ConfigError("SETTLEMENT_METHODS must define at least one method")
        return out

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
