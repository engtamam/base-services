# =============================================================================
# verification-service — Configuration (fail-fast, ZERO-HARDCODED)
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
    SERVICE_NAME: str = "verification-service"
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
    # Salts the subject pseudonym on an erasure ack. FROZEN — config/product.identifiers.lock.json.
    PII_SALT: str = ""
    ERASURE_MAX_SCAN: int = 100_000   # a DSAR is rare, but must never be an unbounded read

    # ── verification (Phase 40) — every provider is CONFIG, never code ────────
    # WHICH provider answers WHICH kind of claim. Same name=value idiom as PRIVACY_TARGETS, so a
    # market we have never heard of is one entry rather than a code change.
    #   "individual=nafath,business=wathq,tax=vies"
    # A claim type with no provider named is one this platform does not verify — and asking for it
    # is refused rather than quietly answered by a default.
    VERIFY_PROVIDERS: str = ""
    # Per-provider profile: url, auth, request mapping, and WHERE the verdict lives in the reply.
    # Written as `provider.key=value` pairs so one flat string configures any number of providers:
    #   "wathq.url=https://…,wathq.auth=bearer:WATHQ_TOKEN,wathq.request=cr={claim},
    #    wathq.verdict_path=data.status,wathq.verdict_ok=VERIFIED,
    #    wathq.verdict_refused=NOT_FOUND,wathq.reference_path=data.ref"
    # We ship no provider endpoints of our own: we do not hold verified contracts for Nafath or
    # Wathq, and inventing them would produce a file that looks authoritative and is wrong.
    VERIFY_PROFILES: str = ""
    # Credential per provider, by SECRET NAME — resolved through the secrets port, never inline.
    #   "wathq=WATHQ_TOKEN,nafath=NAFATH_TOKEN"
    VERIFY_SECRETS: str = ""
    VERIFY_TIMEOUT_SECONDS: int = 15
    # How long a verdict stays trustworthy, per claim type. A commercial registration can be
    # cancelled and a licence can lapse, so "verified once" is not "verified forever". 0 = never
    # expires, which is only right for a claim that cannot change.
    #   "business=365,individual=1095,tax=365"
    VERIFY_TTL_DAYS: str = ""
    # How long we wait for an async provider (a Nafath-style prompt on the user's phone) before
    # the attempt lapses. Lapsing is NOT a verdict of verified — it means nobody answered.
    VERIFY_PENDING_TTL_SECONDS: int = 900
    # Manual review: require two different reviewers to approve. Off by default because it needs
    # two staff, on for markets or claim types where one person granting trust is unacceptable.
    VERIFY_TWO_EYES: bool = False

    # ── audit trail (Phase 38) — where privileged actions are witnessed ───────
    # `null` records NOTHING and says so; the boot guard below refuses it in production, because a
    # service that cannot witness its own privileged actions has no audit trail at all — and an
    # empty hash chain looks like a working control while answering nothing.
    AUDIT_SINK_DRIVER: str = "null"          # null | http | eventbus
    AUDIT_SERVICE_URL: str = ""              # e.g. http://audit-service:8000
    # OUR ingest path is a default, not an assumption about anyone else's platform — the same
    # reasoning as PRIVACY_ERASE_PATH. Nothing in the sink hardcodes it.
    AUDIT_INGEST_PATH: str = "/internal/audit"
    AUDIT_TOPIC: str = "audit.events"        # eventbus sink
    # Short on purpose: this sits on the request path, and a slow audit must not become a slow API.
    AUDIT_TIMEOUT_SECONDS: int = 5
    # What happens when a CRITICAL action cannot be witnessed. Default closed: an action nobody can
    # prove is worse than a refused one for the actions investigations actually turn on (permission
    # grants, credential changes, exports, suspensions). Routine actions always fail open — the
    # split is per action, declared by each service, not a global switch.
    #
    # Turning this off is a deliberate availability-over-evidence trade. It is config so an operator
    # CAN make it during an audit outage, and it is documented so nobody makes it by accident.
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
    def verify_providers(self) -> dict[str, str]:
        """`claim_type -> provider name`. A malformed entry fails at BOOT: a provider we cannot
        resolve is a claim we would silently stop checking."""
        out: dict[str, str] = {}
        for part in self.VERIFY_PROVIDERS.split(","):
            part = part.strip()
            if not part:
                continue
            claim, sep, provider = part.partition("=")
            claim, provider = claim.strip(), provider.strip()
            if not sep or not claim or not provider:
                raise ValueError(f"VERIFY_PROVIDERS entry {part!r} must be claim_type=provider")
            out[claim] = provider
        return out

    @property
    def verify_profiles(self) -> dict[str, dict[str, str]]:
        """`provider -> {key: value}`, parsed from flat `provider.key=value` pairs."""
        out: dict[str, dict[str, str]] = {}
        for part in self.VERIFY_PROFILES.split(","):
            part = part.strip()
            if not part:
                continue
            lhs, sep, value = part.partition("=")
            provider, dot, key = lhs.strip().partition(".")
            if not sep or not dot or not provider or not key:
                raise ValueError(
                    f"VERIFY_PROFILES entry {part!r} must be provider.key=value")
            out.setdefault(provider, {})[key] = value.strip()
        return out

    @property
    def verify_secrets(self) -> dict[str, str]:
        """`provider -> SECRET NAME`. A name, never a value: a credential in a config string ends
        up in a ConfigMap, a log line, and a support ticket."""
        out: dict[str, str] = {}
        for part in self.VERIFY_SECRETS.split(","):
            part = part.strip()
            if not part:
                continue
            provider, sep, key = part.partition("=")
            provider, key = provider.strip(), key.strip()
            if not sep or not provider or not key:
                raise ValueError(f"VERIFY_SECRETS entry {part!r} must be provider=SECRET_NAME")
            if key.startswith("http") or "/" in key:
                raise ValueError(
                    f"VERIFY_SECRETS {key!r} must be a secret NAME, not a value or URL")
            out[provider] = key
        return out

    @property
    def verify_ttl_days(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for part in self.VERIFY_TTL_DAYS.split(","):
            part = part.strip()
            if not part:
                continue
            claim, sep, days = part.partition("=")
            try:
                value = int(days.strip())
            except ValueError:
                raise ValueError(f"VERIFY_TTL_DAYS {days!r} for {claim!r} must be an integer")
            if not sep or value < 0:
                raise ValueError(f"VERIFY_TTL_DAYS entry {part!r} must be claim_type=days (>= 0)")
            out[claim.strip()] = value
        return out

    def assert_verify_config_ok(self) -> None:
        """Boot guard. A verifier that cannot verify must not accept claims it will answer wrongly.

        The specific catastrophe: a claim type is advertised as verified, the provider behind it is
        missing or misconfigured, and every check falls through to whatever the caller does on
        error. If that is 'allow', the platform has an identity check that approves everyone while
        every dashboard shows it enabled.
        """
        providers = self.verify_providers        # parse now — malformed fails at boot
        profiles = self.verify_profiles
        self.verify_secrets
        self.verify_ttl_days

        if not self.is_production:
            return
        for claim, provider in providers.items():
            if provider == "null":
                raise ValueError(
                    f"VERIFY_PROVIDERS maps {claim!r} to the null verifier in production: every "
                    f"check would return error and nothing would ever be verified.")
            if provider in ("manual", "document"):
                continue                          # a human answers these; no URL needed
            url = profiles.get(provider, {}).get("url", "")
            if not url:
                raise ValueError(
                    f"provider {provider!r} verifies {claim!r} but has no url in VERIFY_PROFILES")
            if not url.startswith(("http://", "https://")):
                raise ValueError(f"VERIFY_PROFILES {provider}.url {url!r} must be http(s)")
            if not profiles.get(provider, {}).get("verdict_path"):
                raise ValueError(
                    f"provider {provider!r} has no verdict_path: we would not know where its "
                    f"answer lives, so every response would read as error")

    @property
    def is_production(self) -> bool:
        return self.ENV == "production"

    @property
    def audit_ingest_url(self) -> str:
        """Where audit events go. Empty when no URL is configured — never a guessed default, because
        a wrong host would send the audit trail somewhere the operator cannot read while every call
        still looked successful."""
        if not self.AUDIT_SERVICE_URL:
            return ""
        return self.AUDIT_SERVICE_URL.rstrip("/") + self.AUDIT_INGEST_PATH

    def assert_audit_config_ok(self) -> None:
        """Boot guard. Refuse to start production without somewhere to witness privileged actions.

        The failure this prevents is specific and was real here: audit-service ran for months with
        a complete ingest endpoint and no callers, so the chain verified perfectly and contained
        nothing. Nothing failed, no test went red, and the control existed only on paper. A service
        that cannot record what it does must not quietly serve traffic as though it can.
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
