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
    # What the redis limiter does when Redis cannot be reached. `local` degrades to a per-pod
    # limiter with the same policy — coarser, but alive and still limiting. `closed` denies all
    # (the limiter becomes the outage); `open` allows all (the limit is gone). See phase-32 T4.
    RATELIMIT_DEGRADE: str = "local"            # local | closed | open
    # How a request becomes a key. Composite: each present part contributes, so an authenticated
    # request buckets per subject (fair) and an anonymous one falls through to the IP. Behind a NAT,
    # `ip` alone is one bucket for a whole office — add `subject`/`tenant` to split it. See T7.
    RATELIMIT_KEY_PARTS: str = "subject,ip"     # subject | ip | tenant | route (comma list)
    # Per-route budgets. First matching prefix (longest first) wins; unmatched uses RATELIMIT_MAX.
    # A login surface must be far stricter than a health check, so this is config, not a constant.
    RATELIMIT_ROUTES: str = ""                  # JSON: [{"prefix","class","limit","window_s","cost"}]
    # Which HTTP statuses count as a "failed attempt" on a counted surface. ammar's insight: a
    # correct password on an account pending approval returns 403 and must NOT count, or the user is
    # locked out while waiting for the approval that would let them in. Empty = count every request.
    RATELIMIT_COUNT_STATUSES: str = ""          # e.g. "401" for a login surface; comma list

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
    # ── email verification ────────────────────────────────────────────────────
    # An unproven address is not an identity: without this, anyone may register as
    # cfo@bank.com and every service that treats the address as a person inherits the lie.
    EMAIL_VERIFICATION_ENABLED: bool = True
    # Refuse to MINT a token for an unverified address. Enforced at issue_tokens rather than in
    # the login route, because minting is the only moment anything is checked — everywhere else
    # the claim is believed. A route-level check is one new route away from being bypassed.
    LOGIN_REQUIRES_VERIFIED_EMAIL: bool = True
    # Accounts that existed BEFORE verification shipped. `grandfather` treats a missing flag as
    # verified so an upgrade cannot lock out every existing user; `enforce` treats it as
    # unverified and they must prove the address. Stated, never assumed.
    EMAIL_VERIFICATION_LEGACY: str = "grandfather"    # grandfather | enforce
    EMAIL_VERIFICATION_TTL_SECONDS: int = 86_400      # 24h; a link older than this is refused
    # How many live tokens one account may hold. Every resend mints a credential; unbounded
    # resends mean unbounded live keys, each another chance to leak via a mail archive or proxy.
    EMAIL_VERIFICATION_MAX_OUTSTANDING: int = 3
    # Peppers the stored digest. A thief with the table still cannot recognise a token they
    # obtained elsewhere. Empty degrades to plain SHA-256 rather than disabling verification.
    EMAIL_VERIFICATION_PEPPER: str = ""
    # Where the link points. NEVER derived from the request Host header: an attacker-controlled
    # host would put a live credential in a URL they own.
    EMAIL_VERIFICATION_URL: str = ""      # e.g. https://app.example.com/verify?token=
    EMAIL_VERIFICATION_TEMPLATE: str = "auth.verify_email"
    # Per-ADDRESS cooldown between verification mails. A per-IP rate limit protects the service;
    # it does nothing for the person being mail-bombed, because an attacker simply spreads the
    # requests. The limit that protects the human is keyed on the human.
    EMAIL_VERIFICATION_RESEND_COOLDOWN_SECONDS: int = 60
    NOTIFIER_DRIVER: str = "null"                 # null | http
    NOTIFY_SERVICE_URL: str = ""
    NOTIFY_PATH: str = "/internal/notify"
    NOTIFY_TIMEOUT_SECONDS: int = 5
    # What a SELF-REGISTERED account gets. Comma list, empty by default: a stranger who signs
    # themself up is nobody until somebody grants them something. This exists because the route
    # used to take caps and roles FROM THE REQUEST BODY — an unauthenticated caller could send
    # caps=["*"] and mint a token that satisfied every capability check on the platform.
    #
    # Roles only, never capabilities. Roles are names an operator recognises and the resolver
    # maps to capabilities; letting config name raw caps would rebuild the same hole one file
    # further away, with an operator typing "platform.admin" instead of an attacker.
    REGISTRATION_DEFAULT_ROLES: str = ""
    # The one tenant allowed to hold platform.* capabilities. `platform` has always been a naming
    # convention and never a boundary — nothing refused a platform.* cap on a customer's token.
    # Empty disables the check, which is the pre-existing behaviour and is logged at boot.
    PLATFORM_TENANT: str = ""

    @property
    def registration_default_roles(self) -> list[str]:
        return [r.strip() for r in self.REGISTRATION_DEFAULT_ROLES.split(",") if r.strip()]
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
    IDENTITY_MEMBERS_WRITE_CAP: str = "members:write"   # staffing a tenant is a bigger deal than reading it
    IDENTITY_MEMBERS_PAGE_SIZE: int = 50         # default page
    IDENTITY_MEMBERS_MAX_PAGE_SIZE: int = 200    # a caller may not ask for the whole company at once

    # How a person who does not exist yet gets a password. The PLATFORM decides; we enforce.
    #
    #   invite    (default, secure) the admin creates the account and CANNOT log into it. A
    #             one-time token is returned once and the human sets their own password.
    #   password  the admin sets an initial password. Simpler — and it lets an admin pre-register
    #             an identifier they do not control (`cfo@bank.com`) with a password they know,
    #             then own that login when the real human is hired. Opt-in, eyes open.
    #
    # In BOTH modes an admin may never set the credential of a person who ALREADY exists: it is
    # the key to every other company that person works for (T2).
    IDENTITY_MEMBER_ADD_MODE: str = "invite"     # invite | password
    IDENTITY_INVITE_TTL_SECONDS: int = 604_800   # 7 days; an invite that never expires is a spare key

    # ── where a member's capabilities come from ─────────────────────────────
    # identity owns "Sara is a compliance-manager at acme" — a fact about her employment.
    # authz owns "a compliance-manager may do X, Y, Z"     — policy.
    # Roles are the INPUT; capabilities are DERIVED.
    #
    #   membership  (default) caps are read off the membership row. No authz-service needed — but
    #               editing a role later changes nothing for anyone who already holds it.
    #   authz       caps are resolved from authz at mint. Edit the role, everyone's caps follow.
    IDENTITY_CAPS_SOURCE: str = "membership"     # membership | authz
    AUTHZ_URL: str = ""                          # e.g. http://authz-service:8080
    AUTHZ_INTERNAL_TOKEN: str = ""               # the internal-tier token authz requires
    AUTHZ_TIMEOUT_MS: int = 2000                 # mint is a hot path; it may not hang on authz
    AUTHZ_CAPS_TTL_SECONDS: int = 60             # cache; ALSO the role-change propagation window
    # The WIRE is config too, so identity can point at any authority that answers this question —
    # behind a gateway that renames things, or a different implementation entirely.
    AUTHZ_EFFECTIVE_PATH: str = "internal/authz/effective"
    AUTHZ_TOKEN_HEADER: str = "X-Internal-Token"
    AUTHZ_CAPS_FIELD: str = "capabilities"       # the field the caps come back in
    AUTHZ_SUBJECT_PARAM: str = "subject"
    AUTHZ_TENANT_PARAM: str = "tenant"
    AUTHZ_ROLES_PARAM: str = "roles"
    AUTHZ_ROLES_SEPARATOR: str = ","

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

    @property
    def audit_ingest_url(self) -> str:
        """Where audit events go. Empty when unconfigured — never a guessed default, because a
        wrong host would send the audit trail somewhere the operator cannot read while every call
        still looked successful."""
        if not self.AUDIT_SERVICE_URL:
            return ""
        return self.AUDIT_SERVICE_URL.rstrip("/") + self.AUDIT_INGEST_PATH

    def assert_email_verification_ok(self) -> None:
        """Boot guard. A service that REQUIRES a proven address must be able to ask for proof.

        Enforcing verification with no way to send the mail locks every new account out
        permanently and looks, from the outside, exactly like a broken login. The two settings
        have to agree, and disagreeing must stop the deploy rather than the user.
        """
        if not self.is_production or not self.EMAIL_VERIFICATION_ENABLED:
            return
        if self.LOGIN_REQUIRES_VERIFIED_EMAIL and self.NOTIFIER_DRIVER == "null":
            raise ValueError(
                "LOGIN_REQUIRES_VERIFIED_EMAIL is on but NOTIFIER_DRIVER=null: no verification "
                "mail can be sent, so every new account would be permanently unable to log in. "
                "Set NOTIFIER_DRIVER=http with NOTIFY_SERVICE_URL.")
        if self.NOTIFIER_DRIVER == "http" and not self.NOTIFY_SERVICE_URL:
            raise ValueError("NOTIFIER_DRIVER=http but NOTIFY_SERVICE_URL is empty.")
        if not self.EMAIL_VERIFICATION_URL:
            raise ValueError(
                "EMAIL_VERIFICATION_URL is empty: the mail would carry no link to click. It is "
                "config and never the request Host, or an attacker-controlled host would receive "
                "a live credential.")

    # assert_privacy_config_ok lived here and checked exactly one thing: PII_SALT. That check is
    # now assert_pii_config_ok, which every service runs rather than the two that remembered to
    # write it — so this method had nothing left to do and keeping an empty guard would suggest
    # the erasure path has a boot check of its own when it does not.

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

    @field_validator("IDENTITY_MEMBER_ADD_MODE")
    @classmethod
    def _known_add_mode(cls, v: str) -> str:
        # A typo'd mode must not silently fall back to a default. `IDENTITY_MEMBER_ADD_MODE=invit`
        # quietly selecting `password` would hand every tenant admin the ability to choose other
        # people's passwords — the exact thing the invite mode exists to prevent. Fail at BOOT.
        allowed = ("invite", "password")
        if v not in allowed:
            raise ValueError(
                f"unknown IDENTITY_MEMBER_ADD_MODE {v!r} — allowed: {', '.join(allowed)}"
            )
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the validated singleton settings.

    Raises pydantic.ValidationError at first call if the environment is invalid,
    which surfaces as a hard startup failure (fail-fast).
    """
    return Settings()  # type: ignore[call-arg]  # values are supplied by env
