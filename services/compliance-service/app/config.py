# =============================================================================
# compliance-service — Configuration (fail-fast, ZERO-HARDCODED)
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
    SERVICE_NAME: str = "compliance-service"
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

    # ── compliance-service (Phase 21) — PDPL/GDPR, all config-driven ──────────
    ORCHESTRATOR_DRIVER: str = "null"        # null | eventbus | job-http
    CLOCK_DRIVER: str = "system"             # system | manual (tests)
    JOB_SERVICE_URL: str = ""                # job-http orchestrator target
    JOB_SERVICE_TOKEN: str = ""

    CONSENT_CACHE_TTL_SECONDS: int = 30      # hot path: consent_check
    DSAR_SLA_DAYS: int = 30                  # PDPL/GDPR data-subject request deadline
    BREACH_NOTIFY_HOURS: int = 72            # GDPR/PDPL breach notification clock
    DSAR_VERIFICATION_TTL_SECONDS: int = 86_400
    DATA_RESIDENCY_DEFAULT: str = "SA"       # ISO country of primary storage

    # Salts the subject pseudonym written into every IMMUTABLE record (audit chain, ledgers,
    # invoices). FROZEN — see config/product.identifiers.lock.json. Change it and every pseudonym
    # re-maps: the history orphans and an erasure can no longer prove it erased the right person.
    PII_SALT: str = ""
    COMPLIANCE_LIST_LIMIT: int = 100

    # ── erasure fan-out (Phase 37) — discovery is CONFIG, never code ──────────
    # WHICH services hold a subject's data comes from the data map (runtime rows). WHERE they live
    # comes from here, in the platform's `name=value` idiom (same shape as ADMIN_SERVICES). A
    # customer's own service — in any language, on any platform — is ONE entry; nothing in the
    # dispatcher names a service, including ours.
    PRIVACY_TARGETS: str = ""                # "identity-service=http://identity:8080,acme-crm=https://crm/…"
    # The default path a target serves. `/internal/privacy/erase` is OUR convention, not an
    # assumption about anyone else — override per target when theirs differs.
    PRIVACY_ERASE_PATH: str = "/internal/privacy/erase"
    PRIVACY_ERASE_PATHS: str = ""            # per-target override: "acme-crm=/api/gdpr/forget,…"
    PRIVACY_TRANSPORT_DRIVER: str = "null"   # null | http  (null dispatches nothing and SAYS so)
    PRIVACY_DISPATCH_TIMEOUT_SECONDS: int = 30
    PRIVACY_MAX_ATTEMPTS: int = 8            # then the target is FAILED and VISIBLE, never silent
    PRIVACY_BACKOFF_MS: int = 500            # full-jitter base between attempts
    PRIVACY_CONCURRENCY: int = 5             # max targets driven in parallel per sweep
    # Per-target auth seam. Our own services accept INTERNAL_SERVICE_TOKEN, but a third party has
    # its own credential — and we must never send ours to someone else's host. This maps a target to
    # the NAME of a secret; the value is resolved through the secrets port (env/file/vault), so no
    # credential is ever written in config. Targets with no entry get the internal token.
    PRIVACY_TARGET_SECRETS: str = ""         # "acme-crm=ACME_CRM_ERASE_TOKEN,…"

    @property
    def privacy_targets(self) -> dict[str, str]:
        """`name -> base_url`. Raises ValueError on a malformed entry — a target we cannot reach is
        a DSAR that can never complete, so it must fail at BOOT, not at erasure time."""
        out: dict[str, str] = {}
        for part in self.PRIVACY_TARGETS.split(","):
            part = part.strip()
            if not part:
                continue
            name, sep, url = part.partition("=")
            name, url = name.strip(), url.strip()
            if not sep or not name or not url:
                raise ValueError(f"PRIVACY_TARGETS entry {part!r} must be name=base_url")
            if not (url.startswith("http://") or url.startswith("https://")):
                raise ValueError(f"PRIVACY_TARGETS url {url!r} must start with http:// or https://")
            out[name] = url.rstrip("/")
        return out

    @property
    def privacy_erase_paths(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for part in self.PRIVACY_ERASE_PATHS.split(","):
            part = part.strip()
            if not part:
                continue
            name, sep, path = part.partition("=")
            name, path = name.strip(), path.strip()
            if not sep or not name or not path.startswith("/"):
                raise ValueError(f"PRIVACY_ERASE_PATHS entry {part!r} must be name=/relative/path")
            out[name] = path
        return out

    @property
    def privacy_target_secrets(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for part in self.PRIVACY_TARGET_SECRETS.split(","):
            part = part.strip()
            if not part:
                continue
            name, sep, key = part.partition("=")
            name, key = name.strip(), key.strip()
            if not sep or not name or not key:
                raise ValueError(f"PRIVACY_TARGET_SECRETS entry {part!r} must be name=SECRET_KEY")
            if key.startswith("/") or "://" in key:
                raise ValueError(f"PRIVACY_TARGET_SECRETS {key!r} must be a secret NAME, not a value")
            out[name] = key
        return out

    def secret_key_for(self, service: str) -> str | None:
        """The secret NAME holding this target's credential, or None to use the internal token."""
        return self.privacy_target_secrets.get(service)

    def erase_path_for(self, service: str) -> str:
        """The path THIS target serves — its own override, else the default."""
        return self.privacy_erase_paths.get(service, self.PRIVACY_ERASE_PATH)

    def assert_privacy_config_ok(self) -> None:
        """Boot guard. A dispatcher that can reach nothing can never complete a DSAR, and a DSAR
        that can never complete is a regulatory failure that LOOKS like progress — so refuse to
        start rather than accept requests we cannot finish."""
        self.privacy_targets      # parse now: a malformed entry fails at boot, not mid-erasure
        self.privacy_erase_paths
        if self.is_production and self.PRIVACY_TRANSPORT_DRIVER == "null":
            raise ValueError(
                "PRIVACY_TRANSPORT_DRIVER=null in production: DSARs would be accepted and never "
                "dispatched — set 'http' and register PRIVACY_TARGETS")
        if self.is_production and self.PRIVACY_TRANSPORT_DRIVER == "http" and not self.privacy_targets:
            raise ValueError(
                "PRIVACY_TARGETS is empty in production: every DSAR would hang waiting for targets "
                "that can never be reached")

    @property
    def egress_allowed_hosts(self) -> frozenset[str]:
        return frozenset(
            h.strip().lower() for h in self.EGRESS_ALLOWED_HOSTS.split(",") if h.strip()
        )

    # The status a REJECTED target-ack answers with. 409: the DSAR exists, but this service is
    # not one of its targets — a conflict with the job's state, not a malformed request. What is
    # NOT configurable is that it is a 4xx: a refusal must never be indistinguishable from success.
    COMPLIANCE_ACK_REJECT_STATUS: int = 409
    # The DSAR itself does not exist. Config too — what is NOT configurable is that both are 4xx.
    COMPLIANCE_ACK_NOTFOUND_STATUS: int = 404

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
