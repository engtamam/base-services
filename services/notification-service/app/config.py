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

    # ── CORS — who may call this service FROM A BROWSER ─────────────────────
    # Empty by default, and that default is a real choice rather than an omission: with no origins
    # configured no CORS headers are emitted at all, so a browser blocks every cross-origin call
    # and a server-to-server caller is unaffected. Opening a hole must be a decision somebody
    # typed, not the state a service arrives in.
    #
    # There was no CORS anywhere in this platform, which is invisible from a terminal and fatal
    # from a product: curl works, every fetch() from a client app or an operator panel is refused
    # by the browser before our code sees the request, and no server-side test catches it because
    # the rule is enforced in the browser.
    #
    # `*` IS REFUSED when credentials are allowed — the browser refuses that combination too, so
    # accepting it here would only move the failure somewhere harder to read. Serving the app and
    # the API from one origin remains the better answer where it is possible; this is for the
    # deployments where it is not.
    CORS_ALLOWED_ORIGINS: str = ""            # comma list of exact origins, e.g. https://app.example.com
    CORS_ALLOW_CREDENTIALS: bool = False      # cookies / Authorization on cross-origin calls
    CORS_ALLOWED_METHODS: str = "GET,POST,PUT,PATCH,DELETE,OPTIONS"
    CORS_ALLOWED_HEADERS: str = "authorization,content-type,x-request-id"
    CORS_MAX_AGE_SECONDS: int = 600           # how long a browser may cache the preflight

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.CORS_ALLOWED_ORIGINS.split(",") if o.strip()]

    @property
    def cors_methods(self) -> list[str]:
        return [m.strip().upper() for m in self.CORS_ALLOWED_METHODS.split(",") if m.strip()]

    @property
    def cors_headers(self) -> list[str]:
        return [h.strip() for h in self.CORS_ALLOWED_HEADERS.split(",") if h.strip()]

    def assert_cors_config_ok(self) -> None:
        """Boot guard. A CORS policy that cannot mean what it says must not start.

        `*` with credentials is refused by every browser, so a service configured that way would
        serve headers that look permissive and work for nobody — the hardest kind of failure to
        diagnose, because the configuration and the symptom disagree.
        """
        if "*" in self.cors_origins and self.CORS_ALLOW_CREDENTIALS:
            raise ValueError(
                "CORS_ALLOWED_ORIGINS='*' with CORS_ALLOW_CREDENTIALS=true is refused by every "
                "browser, so this would emit headers that satisfy nothing. List the exact origins "
                "that need credentials.")
        if self.is_production and "*" in self.cors_origins:
            raise ValueError(
                "CORS_ALLOWED_ORIGINS='*' in production lets any site on the internet call this "
                "service with a user's browser. List the origins you actually serve.")

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
    # How long a client should wait before retrying an action we REFUSED because the audit log
    # was unreachable. It is a 503 + Retry-After, never a 500: the action did not happen, nothing
    # committed, and trying again later is the correct response. A 500 would tell the caller we
    # crashed, which invites a bug report instead of a retry.
    AUDIT_RETRY_AFTER_SECONDS: int = 30

    # ── consent gate — do not send what nobody agreed to receive ──────────────
    # Preferences are the user's CHANNEL opt-out. Consent is the LAWFUL BASIS for a purpose, and
    # they are not the same question: a person who never opted out of email may still never have
    # consented to marketing. This service had the first and not the second.
    # Templates this deployment ships with, seeded at boot. JSON list of
    #   [{"key","locale","subject","body"}]
    #
    # Content lives in CONFIG, never in code: a template is a product's words, in a product's
    # language, and hardcoding one here would make every platform inherit ours. Seeding matters
    # because a flow that depends on a template nobody created fails at the worst moment — a
    # verification mail that is accepted, then silently never rendered, leaves an account unable
    # to log in with nothing to show why.
    #
    # Idempotent: an upsert on every boot, so re-deploying re-asserts the shipped copy and an
    # operator's later edit through the API is overwritten only by a deliberate config change.
    NOTIFY_SEED_TEMPLATES: str = ""

    @property
    def notify_seed_templates(self) -> list[dict[str, str]]:
        import json as _json
        raw = (self.NOTIFY_SEED_TEMPLATES or "").strip()
        if not raw:
            return []
        try:
            parsed = _json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"NOTIFY_SEED_TEMPLATES is not valid JSON: {exc}") from exc
        if not isinstance(parsed, list):
            raise ValueError('NOTIFY_SEED_TEMPLATES must be a LIST of template objects')
        for entry in parsed:
            if not isinstance(entry, dict) or not {"key", "locale", "subject", "body"} <= set(entry):
                raise ValueError('each template needs key, locale, subject and body')
        return parsed

    CONSENT_GATE_DRIVER: str = "null"        # null | http
    COMPLIANCE_SERVICE_URL: str = ""
    CONSENT_CHECK_PATH: str = "/internal/compliance/consent-check"
    CONSENT_TIMEOUT_SECONDS: int = 3
    CONSENT_GATED: bool = True               # this service DOES process personal data by purpose
    # Which notifications need consent, and under which purpose name. JSON, because the mapping is
    # a product decision that differs per platform: {"event_or_template": "purpose"}. Anything not
    # listed is transactional (an OTP, a receipt, a password reset) and is sent on a different
    # lawful basis — contract, not consent — so it must NOT be gated or the platform breaks.
    NOTIFY_CONSENT_PURPOSES: str = ""
    # What to do when the gate answers UNKNOWN. Closed by default: the gated notifications are by
    # definition the ones a regulator would ask about, and an unsent marketing email is a smaller
    # harm than one sent without a lawful basis.
    NOTIFY_CONSENT_UNKNOWN: str = "fail_closed"      # fail_closed | fail_open

    @property
    def notify_consent_purposes(self) -> dict[str, str]:
        """`{"event_or_template": "purpose"}`. Parsed at boot so a malformed map fails there."""
        import json as _json
        raw = (self.NOTIFY_CONSENT_PURPOSES or "").strip()
        if not raw:
            return {}
        try:
            parsed = _json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"NOTIFY_CONSENT_PURPOSES is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()):
            raise ValueError('NOTIFY_CONSENT_PURPOSES must be {"event_or_template": "purpose"}')
        return parsed
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

    def assert_consent_config_ok(self) -> None:
        """Boot guard. A service that gates processing on consent must be able to ask.

        Only applies when the SERVICE declares CONSENT_GATED — the whole point of that flag is to
        distinguish "this service has no consent-gated processing" from "somebody forgot to
        configure the gate". Those are identical from the driver alone, and that ambiguity is how
        consent-check ran for four phases with no callers and nothing ever went red.
        """
        if not self.is_production or not self.CONSENT_GATED:
            return
        if self.CONSENT_GATE_DRIVER == "null":
            raise ValueError(
                "CONSENT_GATE_DRIVER=null in production, but this service declares consent-gated "
                "processing: every check would answer UNKNOWN and the decision would fall to each "
                "call site's fallback, which is not where a compliance posture should be decided. "
                "Set 'http' with COMPLIANCE_SERVICE_URL."
            )
        if self.CONSENT_GATE_DRIVER == "http" and not self.COMPLIANCE_SERVICE_URL:
            raise ValueError(
                "CONSENT_GATE_DRIVER=http but COMPLIANCE_SERVICE_URL is empty: every consent "
                "question would fail to send and answer UNKNOWN while the service looked healthy."
            )
        if (self.COMPLIANCE_SERVICE_URL
                and not self.COMPLIANCE_SERVICE_URL.startswith(("http://", "https://"))):
            raise ValueError(
                f"COMPLIANCE_SERVICE_URL {self.COMPLIANCE_SERVICE_URL!r} must start with "
                f"http:// or https://"
            )

    def assert_pii_config_ok(self) -> None:
        """Boot guard. Refuse to start production without the salt that pseudonyms are built from.

        Every service in the platform writes `subject_ref` — the per-tenant pseudonym that lets an
        immutable audit row, ledger entry or invoice outlive the person named in it. It is
        HMAC(PII_SALT, tenant|subject), and `core.pii.subject_ref` REFUSES an empty salt rather
        than emitting an unsalted digest that anyone could reverse by hashing every email address.

        That refusal is correct and badly timed. Without this guard the service starts, passes
        every health check, serves traffic for weeks, and then raises the first time a real person
        touches a path that pseudonymises — which is a 500 in production, at whatever hour the
        first DSAR or first audited action happens to arrive. A misconfiguration must refuse to
        START, not fail one request months later.

        Two services already did this. Nineteen declared PII_SALT and never checked it, which is
        why safety:pii-salt now fails the build for any service that reads the salt without
        guarding it.

        NOT CHECKED HERE, because it cannot be: that every service was given the SAME salt. Two
        different non-empty salts fold one person to two pseudonyms, and every service still boots
        and still works. That is a deployment invariant — see the chart's shared-secret handling
        and config/product.identifiers.lock.json, which freezes the value for exactly this reason.
        """
        if self.is_production and not self.PII_SALT:
            raise ValueError(
                "PII_SALT is empty in production: subject pseudonyms would be unsalted, which "
                "makes them reversible by guessing every email address — not pseudonyms at all. "
                "Any audited or pseudonymised write would 500 the first time it was reached. Set "
                "PII_SALT (it is FROZEN — see config/product.identifiers.lock.json; changing it "
                "re-maps every pseudonym and orphans the history), and set the SAME value in "
                "every service or one person becomes two."
            )

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
