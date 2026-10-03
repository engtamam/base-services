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

import json as _json

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _json_any_map(raw: str) -> dict:
    """Parse a JSON object env var into a dict, tolerating empty/malformed input by returning {} —
    a mis-set map must not crash boot; the driver it feeds simply sees no keys."""
    if not raw or not raw.strip():
        return {}
    try:
        val = _json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return val if isinstance(val, dict) else {}


def _json_str_map(raw: str) -> dict[str, str]:
    return {str(k): str(v) for k, v in _json_any_map(raw).items() if k and v}


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
    # ── Internal CALLER identity (pluggable; default preserves today's behaviour) ──────────────
    # Who may reach THIS service's /internal tier, and how their identity is proven. `shared` = the
    # flat token only (today). `static`/`jwks` verify a short-TTL RS256 assertion (iss=caller,
    # aud=this service) against a provisioned public key / the caller's JWKS — verifiable per-caller
    # identity. Default `shared` so an upgrade changes nothing; a deployment opts up when ready.
    INTERNAL_CALLER_DRIVER: str = "shared"          # shared | static | jwks
    # The assertion audience this service requires; empty => SERVICE_NAME (the natural identifier).
    INTERNAL_CALLER_AUDIENCE: str = ""
    # static driver: JSON map {caller_service: PEM_public_key}. jwks driver: JSON map
    # {caller_service: jwks_url}. Only the one matching the driver is read.
    INTERNAL_CALLER_PUBLIC_KEYS_JSON: str = ""
    INTERNAL_CALLER_JWKS_JSON: str = ""
    # During migration a request with NO assertion but the correct flat token is still accepted as an
    # unnamed peer, so enabling static/jwks fleet-wide breaks nothing. Flip OFF once callers all sign.
    INTERNAL_CALLER_ALLOW_TOKEN_FALLBACK: bool = True
    # Per-route caller allow-list: JSON map {"/internal/...": ["caller-a", "caller-b"]} (or ["*"] for
    # any authenticated caller). A route absent here admits any authenticated caller (default-open),
    # so tightening is route-by-route and never breaks a path on upgrade. Longest-prefix match.
    INTERNAL_ROUTE_POLICY_JSON: str = ""
    # CALLER side: this service's own RS256 private key for signing the assertions IT presents when
    # calling other services' /internal tiers, its kid (for jwks rotation), and the assertion TTL.
    # Empty key => this service signs nothing and relies on the flat-token fallback (today's behaviour).
    INTERNAL_ASSERTION_PRIVATE_KEY: str = ""
    INTERNAL_ASSERTION_KID: str = ""
    INTERNAL_ASSERTION_TTL_SECONDS: int = 60

    # ── Backup (backup-service orchestrates; this service produces sealed archives) ──────────
    # Seals backup archives at rest. SEPARATE from any live-secrets key by design: restoring must
    # not require the key a running service uses for its own live secrets. Empty means this service
    # cannot produce a backup, and its export endpoint refuses rather than writing one in the clear.
    # SHARED: it must be the SAME value backup-service holds (in the class of PII_SALT /
    # INTERNAL_SERVICE_TOKEN); gen-secrets.py mints it consistently. Two different values and the
    # archives backup-service collects from this service cannot be opened.
    BACKUP_KEK: str = ""
    # The most rows a single collection may hold before a backup FAILS rather than reads on
    # unbounded — a real ceiling an operator sets to the size the data actually reaches.
    BACKUP_MAX_ROWS_PER_COLLECTION: int = 5_000_000

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
    # Retrying a transient audit failure is safe because every emit carries an idempotency key (a
    # repeat is deduped by the log, never appended twice). Small by design: this sits on the request
    # path, so 2 attempts with a short backoff turns one slow call into a second chance without
    # spending the whole budget the timeout protects.
    AUDIT_MAX_ATTEMPTS: int = 2
    AUDIT_RETRY_BACKOFF_SECONDS: float = 0.2
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
    # ── Postgres connection pool ──────────────────────────────────────────────────────────────
    # asyncpg's default pool is min_size=10, max_size=10, and min is a LAZY-but-STICKY floor: the
    # pool fills to it on first use and holds there for the process lifetime. Left at the default,
    # 24 services each pin 10 backend connections — 240 against a single postgres whose stock ceiling
    # is 100 — and the fleet deadlocks on an ordinary request once enough services have been touched
    # (with maxUnavailable:0, a rollout deadlocks outright: a new pod needs its 10 slots to go Ready
    # while the old pod still holds its 10). So the floor is 1: a pod is Ready on one connection, an
    # idle service costs one, and horizontal scale spends the ceiling deliberately, not by default.
    DATASTORE_POOL_MIN: int = 1
    DATASTORE_POOL_MAX: int = 10           # load-tested ceiling; 5 exhausted under fleet-wide concurrent load while postgres sat idle (PgBouncer multiplexes)
    DATASTORE_POOL_TIMEOUT_SECONDS: float = 10.0            # wait for a free slot, then fail cleanly
    DATASTORE_POOL_MAX_INACTIVE_LIFETIME_SECONDS: float = 300.0  # return idle connections to postgres
    DATASTORE_STATEMENT_CACHE_SIZE: int = 0   # 0 => PgBouncer transaction-mode safe (no asyncpg prepared-statement cache)

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
    # Socket connect/read timeout for the redis limiter, in seconds. Bounds how long a single GCRA
    # call waits on the Redis SOCKET before raising — which is what lets an unreachable Redis hit the
    # degrade path above instead of hanging the request past the gateway's upstream timeout (a 504).
    # Deliberately SHORT: this path exists to fail FAST into the fallback, not to wait.
    RATELIMIT_COMMAND_TIMEOUT_S: float = 1.0
    # How a request becomes a key. Composite: each present part contributes, so an authenticated
    # request buckets per subject (fair) and an anonymous one falls through to the IP. Behind a NAT,
    # `ip` alone is one bucket for a whole office — add `subject`/`tenant` to split it. See T7.
    RATELIMIT_KEY_PARTS: str = "subject,ip"     # subject | ip | tenant | route (comma list)
    # Per-route budgets. First matching prefix (longest first) wins; unmatched uses RATELIMIT_MAX.
    # A login surface must be far stricter than a health check, so this is config, not a constant.
    RATELIMIT_ROUTES: str = ""                  # JSON: [{"prefix","class","limit","window_s","cost"}]
    # Which HTTP statuses count as a "failed attempt" on a counted surface. The reasoning: a
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
    # one account; `phone` folds every spelling of a number to one canonical E.164 so one
    # human is one account. A federated provider is a new value here.
    # A single kind, or a COMMA-LIST to accept several (e.g. "phone,email"): a value is routed to
    # the first kind that accepts it, and the key is namespaced by kind so they never collide.
    IDENTITY_LOGIN_FIELD: str = "username"       # username | email | phone | "phone,email" | …
    # The country calling code applied to a NATIONAL phone number (leading 0 / no country code).
    # Digits only; config, not hardcoded, so phone login works for any market. 966 = KSA.
    IDENTITY_PHONE_DEFAULT_CC: str = "966"

    # Salts the subject pseudonym written into every IMMUTABLE record (audit chain, ledgers,
    # invoices). FROZEN — see config/product.identifiers.lock.json. Change it and every pseudonym
    # re-maps: the history orphans and an erasure can no longer prove it erased the right person.
    PII_SALT: str = ""
    TOKEN_ISSUER_DRIVER: str = "null"           # null(deny) | hs256 | rs256 (identity SIGNS)
    TOKEN_SIGNING_SECRET: str = ""              # hs256 signing secret (>=32)
    TOKEN_PRIVATE_KEY: str = ""                 # rs256 PEM private key
    ACCESS_TTL_SECONDS: int = 900               # 15 min access token
    REFRESH_TTL_SECONDS: int = 604_800          # 7 day refresh token
    # ── automatic signing-key rotation (rs256 + SECRETS_KEK) ───────────────────────
    # Off by default: a deployment keeps signing with the single TOKEN_PRIVATE_KEY exactly as today.
    # Turned on, TOKEN_PRIVATE_KEY is ADOPTED as the first key, published in a JWKS, and rotated on a
    # cron — old public keys stay verifiable through an overlap so no live token is ever rejected.
    # Requires SECRETS_KEK (the ring is sealed at rest) and a jwks verifier on the fleet.
    #
    # TO ENABLE ROTATION you set this true AND schedule the rotation cron — the two go together, or a
    # cron on a disabled deployment POSTs to a 503 and reports failed nightly. The cron is NOT
    # generated into the values file (deploy/services/identity-service.values.yaml is generated from
    # product.json and left with no cronJobs, since rotation is opt-in). Add it at deploy time, e.g.:
    #     cronJobs:
    #       - name: rotate-signing-key
    #         schedule: "30 3 * * *"      # daily; the min-age guard makes a repeat/retry a safe no-op
    #         timeZone: "Asia/Riyadh"
    #         path: /internal/signing-key/rotate
    # The endpoint is idempotent and each retired public key stays in the JWKS for the overlap window
    # (sized off the 7-day refresh TTL), so a daily fire yields one rotation/day and no token is lost.
    TOKEN_KEY_ROTATION_ENABLED: bool = False
    # How long a superseded public key stays in the JWKS. MUST exceed the longest-lived token, which
    # is the REFRESH token (not the access token) — 0 derives it as REFRESH_TTL + 1h of margin.
    TOKEN_SIGNING_OVERLAP_SECONDS: int = 0
    # A rotation is a no-op if the current key is younger than this, so a repeated/retried cron does
    # not mint endless keys. Sized well under the cadence (default 1h vs a 24–72h cadence).
    TOKEN_KEY_ROTATION_MIN_AGE_SECONDS: int = 3600
    # How often each identity replica re-reads the sealed ring (picks up a rotation done elsewhere).
    TOKEN_KEY_REFRESH_SECONDS: int = 45
    MFA_DRIVER: str = "null"                    # null (off) | totp
    MFA_ISSUER_NAME: str = "base-services"      # shown in authenticator apps
    MFA_TOTP_WINDOW: int = 1                     # TOTP clock-skew tolerance (± steps)
    REGISTRATION_ENABLED: bool = True           # open signup vs admin-only
    # The authoritative, FAIL-CLOSED list of how a new person may onboard. A comma-list of known
    # methods: open | invite_token | join_request | invite_link | temp_password. Empty = derive from
    # the legacy knobs (open follows REGISTRATION_ENABLED, admin-invite stays on) so nothing changes
    # until a deployment opts in. An unknown method here fails at BOOT, never silently. See
    # app/core/onboarding.py.
    ONBOARDING_METHODS: str = ""
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

    # ── OTP: a channel-agnostic one-time code (prove a phone, an email, a number at a counter) ──
    # Own salt so a leaked OTP store can't be correlated with any other digest table. Falls back to
    # PII_SALT only to avoid an empty salt in dev; production should set it explicitly.
    OTP_SALT: str = ""
    OTP_CODE_LENGTH: int = 6                     # digits; >=4 enforced
    OTP_TTL_SECONDS: int = 300                   # a code lives 5 min by default
    OTP_MAX_ATTEMPTS: int = 5                    # wrong tries before the code is burned
    OTP_RESEND_COOLDOWN_SECONDS: int = 60        # min gap between two sends to one identifier
    OTP_MAX_PER_HOUR: int = 5                    # hourly cap per identifier (anti mail/SMS-bomb)
    # Delivery: identity owns issue/verify, notification-service owns the wire. The template maps to
    # a channel THERE; the channel hint travels as a variable so one template can fan to sms|whatsapp
    # |email by the recipient's shape. Empty template ⇒ delivery disabled (issue still returns a code
    # to a trusted internal caller, e.g. a test harness or a bring-your-own-sender).
    OTP_TEMPLATE: str = "auth_otp"               # Meta template names: lowercase + digits + underscore ONLY (no dots)
    OTP_DELIVERY_CHANNEL: str = ""               # "", sms, whatsapp, email — "" lets notification pick
    # Which tenant the OTP send resolves its template + provider credentials under, at
    # notification-service. Default "" = today's behavior. A PLATFORM deployment whose admin console
    # registers its WhatsApp templates/credentials under a reserved tenant (e.g. "_platform") sets
    # this to THAT tenant, or the OTP send looks up under "" and never finds them. Config, not a
    # literal, so an operator can change it without a code change.
    OTP_NOTIFY_TENANT: str = ""
    # NOTE: recycled-number policy (a reused phone is a DIFFERENT person) is enforced at person-bind
    # time, which lives in the onboarding release — the knob lands there with the code that reads it,
    # not here as an advertised-but-unread setting.

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
    # Deliberate opt-out for a genuinely single-tenant deployment that RUNS multi-tenant membership
    # but intends '*'/'platform.*' to remain usable (there is no customer to fence off from the
    # platform, because there is only one tenant). Default False makes the ambiguous case —
    # multi-tenant membership on, PLATFORM_TENANT unset — fail closed at boot rather than fail open
    # at mint (r2-05): the platform boundary the mint strip depends on would otherwise be undecidable.
    # Setting this True is the operator saying, in writing, "'*' is meant to work here."
    IDENTITY_ALLOW_UNBOUNDED_PLATFORM: bool = False

    @property
    def registration_default_roles(self) -> list[str]:
        return [r.strip() for r in self.REGISTRATION_DEFAULT_ROLES.split(",") if r.strip()]

    @property
    def member_grantable_caps(self) -> frozenset[str]:
        """The allow-list of directly-grantable caps (only consulted when the policy is `allowlist`)."""
        return frozenset(c.strip() for c in self.IDENTITY_MEMBER_GRANTABLE_CAPS.split(",") if c.strip())
    PASSWORD_MIN_LENGTH: int = 8                 # minimum password length policy
    # Per-account brute-force lockout (generic rate-limit doesn't stop targeted stuffing).
    LOGIN_MAX_ATTEMPTS: int = 5                  # consecutive failures before lockout
    LOGIN_ATTEMPT_WINDOW_SECONDS: int = 900      # window the failures are counted in
    LOGIN_LOCKOUT_SECONDS: int = 900            # how long the account stays locked
    # Account-scoped lockout for the multi-tenant person login: counts failures for one identifier
    # across EVERY source IP, so distributed credential stuffing (each IP staying under the per-source
    # cap) still trips a lock. Deliberately a HIGHER threshold and window than the per-source counter
    # — the per-source counter stays the first line and preserves availability (one attacker source
    # cannot lock a consultant out everywhere); this is the additional per-account signal (r2-04).
    LOGIN_ACCOUNT_MAX_ATTEMPTS: int = 50         # failures across ALL sources before an account lock
    LOGIN_ACCOUNT_WINDOW_SECONDS: int = 3600     # window the cross-source failures are counted in
    LOGIN_ACCOUNT_LOCKOUT_SECONDS: int = 3600    # how long the account stays locked (all sources)

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
    # How DIRECT capabilities on a membership are governed at the human admin routes (add/update
    # member). Direct caps are unioned into the minted token, so an ungoverned grant is a privilege
    # escalation. `subset` (default): a caller may only grant caps it holds ITSELF. `off`: no direct
    # caps at all — authority is roles-only (the strictest posture; a caller passing caps is refused).
    # `allowlist`: caps must be within (the caller's own caps ∩ IDENTITY_MEMBER_GRANTABLE_CAPS). The
    # internal bootstrap (seed-owner, internal add-member) is exempt — it is platform authority.
    IDENTITY_MEMBER_CAPS_POLICY: str = "subset"   # subset | off | allowlist
    IDENTITY_MEMBER_GRANTABLE_CAPS: str = ""       # comma list, used only when policy = allowlist

    # ── Machine identity (OAuth2 client-credentials, RFC 6749 §4.4) ───────────────────────────
    # A tenant-scoped service account for a partner's own code — a POS, an ERP — that calls our API
    # with no browser and no person behind it. Opt-in: OFF by default, because it is a new class of
    # credential and an operator should turn it on deliberately. When off, the routes 404.
    SERVICE_ACCOUNTS_ENABLED: bool = False
    # The token a client-credentials exchange returns. Short by design — a machine re-requests
    # cheaply, and a shorter life bounds the blast radius of a leaked token (NIST SP 800-63B).
    SERVICE_ACCOUNT_TOKEN_TTL_SECONDS: int = 3600      # 1h machine access token
    # Managing service accounts is its OWN capability, distinct from staffing humans: minting a
    # credential that can act on the API is not the same authority as inviting a colleague. The
    # capability STRING is the platform's to choose (config), this only enforces it.
    SERVICE_ACCOUNTS_READ_CAP: str = "service_accounts:read"
    SERVICE_ACCOUNTS_WRITE_CAP: str = "service_accounts:write"
    # A ceiling per tenant, so "issue a service account" cannot become an unbounded write. A real
    # limit an operator sets to the number of integrations a tenant actually runs (ISO 27001 A.8.2).
    SERVICE_ACCOUNTS_MAX_PER_TENANT: int = 100
    SERVICE_ACCOUNTS_PAGE_SIZE: int = 50
    # Default overlap on a secret ROTATION: the previous secret keeps working for this long so a
    # fleet embedding the credential (a POS build on hundreds of tills that each update on their own
    # schedule) migrates without a flag-day outage. It expires ON ITS OWN — an overlap an operator
    # must end by hand is one nobody ends. A rotate may override it (0 = immediate cutover, the right
    # choice for a leak). Default 7 days: long enough for a slow-updating fleet, bounded so a rotated
    # secret does not live indefinitely.
    SERVICE_ACCOUNT_ROTATION_OVERLAP_SECONDS: int = 604_800

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

    # ── legal-acceptance enforcement (ToS / Privacy) ──────────────────────────
    # ToS/Privacy used to be a POST-LOGIN UI nag only: a shop could register and use the API with no
    # acceptance record. This makes acceptance a real SERVER-SIDE precondition, enforced at the mint.
    #   off   — the default: no compliance dependency, behaves as before enforcement existed.
    #   flag  — mint the token normally but stamp `legal_pending:[...]` (claim + login response).
    #   block — refuse the normal token (403) with the owed docs and a RESTRICTED accept token that
    #           can ONLY view/accept legal (caps below), so the shop can accept then re-login.
    # Left `off` by default so enabling is an explicit, non-breaking opt-in; `block` is the strongest
    # for a legal/dispute posture and is one env var away.
    IDENTITY_LEGAL_ENFORCEMENT: str = "off"       # off | flag | block
    COMPLIANCE_URL: str = ""                       # e.g. http://compliance-service:8080
    COMPLIANCE_INTERNAL_TOKEN: str = ""            # the internal-tier token compliance requires
    IDENTITY_LEGAL_GATE_TIMEOUT_MS: int = 3000     # mint is a hot path; do not hang on compliance
    # TTL for the outstanding cache. DEFAULT 0 (no cache) on purpose: a cached "outstanding" would
    # keep blocking a user for up to the TTL AFTER they accepted, re-looping them. Raise only to
    # dedupe bursts, accepting that acceptance then takes up to TTL to be reflected.
    IDENTITY_LEGAL_GATE_TTL_SECONDS: int = 0
    # What identity does if compliance is unreachable. `open` (default) mints anyway and logs+audits
    # the skip — a consent check must not be able to 503 every login on a compliance blip; a brief
    # gap is no worse than the pre-enforcement world. `closed` refuses the mint (max legal strength).
    IDENTITY_LEGAL_GATE_FAIL_MODE: str = "open"    # open | closed
    # The caps the restricted block-mode accept token carries — enough to view + accept legal and
    # nothing else. compliance's /me/legal routes require `compliance.self`.
    IDENTITY_LEGAL_ACCEPT_CAPS: str = "compliance.self"   # comma-separated
    IDENTITY_LEGAL_ACCEPT_TTL_SECONDS: int = 900   # short; it exists only to complete acceptance
    IDENTITY_LEGAL_OUTSTANDING_PATH: str = "internal/compliance/legal/outstanding"
    IDENTITY_LEGAL_TOKEN_HEADER: str = "X-Internal-Token"
    IDENTITY_LEGAL_OUTSTANDING_FIELD: str = "outstanding"
    IDENTITY_LEGAL_SUBJECT_PARAM: str = "subject"
    IDENTITY_LEGAL_TENANT_PARAM: str = "tenant"

    @property
    def legal_accept_caps(self) -> list[str]:
        return [c.strip() for c in self.IDENTITY_LEGAL_ACCEPT_CAPS.split(",") if c.strip()]

    # ── tenant-status enforcement — a suspended/deleted tenant mints NO token ──
    # Until now tenant.status was enforced only by tenant-service's own edit routes: a suspended or
    # deleted tenant's users could still log in and use everything. This gates the MINT (login,
    # refresh, tenant select). Opt-in (default off); `block` refuses the mint. Complements the
    # api-gateway per-request gate — identity stops new tokens, the gateway kills live ones.
    IDENTITY_TENANT_STATUS_ENFORCEMENT: str = "off"   # off | block
    TENANT_SERVICE_URL: str = ""                        # e.g. http://tenant-service:8000
    IDENTITY_TENANT_GATE_TIMEOUT_MS: int = 3000         # mint is a hot path; do not hang on tenant-service
    IDENTITY_TENANT_GATE_TTL_SECONDS: int = 30          # cache + enforcement lag: a suspend is live within this
    IDENTITY_TENANT_GATE_FAIL_MODE: str = "open"        # open (availability) | closed (max strength)
    # A real active tenant never resolves to unknown (tenant-service tombstones deleted ones as a
    # readable 200); unknown is a 404 — a purged/nonexistent tenant a valid mint should not target.
    IDENTITY_TENANT_BLOCK_UNKNOWN: bool = True
    IDENTITY_TENANT_TOKEN_HEADER: str = "X-Internal-Token"

    def assert_tenant_status_config_ok(self) -> None:
        """Boot guard. A declared `block` enforcement that cannot resolve a status reads as enforcement
        while minting for suspended tenants anyway — refuse to boot unless the URL is wired."""
        if self.IDENTITY_TENANT_STATUS_ENFORCEMENT not in ("off", "block"):
            raise ValueError(
                f"IDENTITY_TENANT_STATUS_ENFORCEMENT {self.IDENTITY_TENANT_STATUS_ENFORCEMENT!r} "
                "must be 'off' or 'block'")
        if self.IDENTITY_TENANT_GATE_FAIL_MODE not in ("open", "closed"):
            raise ValueError(
                f"IDENTITY_TENANT_GATE_FAIL_MODE {self.IDENTITY_TENANT_GATE_FAIL_MODE!r} "
                "must be 'open' or 'closed'")
        if self.IDENTITY_TENANT_STATUS_ENFORCEMENT == "block":
            if not self.TENANT_SERVICE_URL:
                raise ValueError(
                    "IDENTITY_TENANT_STATUS_ENFORCEMENT=block requires TENANT_SERVICE_URL to reach "
                    "/internal/tenants/{id}")
            if not self.TENANT_SERVICE_URL.startswith(("http://", "https://")):
                raise ValueError(
                    f"TENANT_SERVICE_URL {self.TENANT_SERVICE_URL!r} must start with http:// or https://")

    # ── operator-managed credentials ──────────────────────────────────────────
    # The key that seals every operator-stored credential. It stays in the ENVIRONMENT and is never
    # settable through any API: a key that protects the secrets and is itself editable in the panel
    # protects nothing from someone who has the panel. Empty means the operator surface refuses
    # (503) rather than storing a credential in the clear.
    SECRETS_KEK: str = ""
    SECRETS_ROTATION_WINDOW_SECONDS: int = 300
    # How long a resolved credential is reused before the store is read again. Mint is the hot path
    # — login, tenant switch and every refresh — and a datastore read plus a scrypt-backed decrypt
    # there, to support a change made a few times a year, is a cost every session would pay.
    AUTHZ_CREDENTIAL_REFRESH_SECONDS: int = 30

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

    def assert_token_audience_ok(self) -> None:
        """Boot guard (identity-005). When multiple services verify against the SAME identity
        JWKS/public key (the documented fleet model), an unbound audience means a token minted for
        service A is accepted by service B — a confused-deputy hole. The verifier only enforces aud
        when TOKEN_AUDIENCE is set, and it defaults to empty. So production with an asymmetric verifier
        (rs256/jwks) MUST bind an audience."""
        if not self.is_production:
            return
        if self.TOKEN_VERIFIER_DRIVER in ("rs256", "jwks") and not self.TOKEN_AUDIENCE.strip():
            raise ValueError(
                f"TOKEN_VERIFIER_DRIVER={self.TOKEN_VERIFIER_DRIVER} but TOKEN_AUDIENCE is empty in "
                "production: tokens are verified with aud unbound, so a token minted for one service is "
                "accepted by every service sharing the key. Set TOKEN_AUDIENCE.")

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

    def assert_otp_salt_ok(self) -> None:
        """Boot guard (identity-010). OTP_SALT falls back to PII_SALT so dev never runs unsalted, but in
        production the OTP-digest salt must be SET and DISTINCT from PII_SALT — otherwise a leaked OTP
        store correlates one-to-one with every other PII_SALT digest table, which is the exact
        cross-table linkage the separate salt exists to prevent."""
        if not self.is_production:
            return
        if not self.OTP_SALT:
            raise ValueError(
                "OTP_SALT is empty in production: it silently falls back to PII_SALT, so OTP digests "
                "share a salt with every other pseudonym table and a leaked OTP store becomes a "
                "correlation key. Set a dedicated OTP_SALT.")
        if self.OTP_SALT == self.PII_SALT:
            raise ValueError(
                "OTP_SALT must be DISTINCT from PII_SALT in production — sharing it defeats the "
                "separate-salt isolation (a leaked OTP store would correlate with subject pseudonyms).")

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

    def assert_operator_config_ok(self) -> None:
        """Boot guard for the operator credential surface.

        Checked in every environment, not just production: a refresh window that outlives the
        rotation window is wrong wherever it is set, and a rule that only bites in production is
        one nobody discovers until the rotation that needed it.
        """
        if self.SECRETS_KEK and (
                self.AUTHZ_CREDENTIAL_REFRESH_SECONDS > self.SECRETS_ROTATION_WINDOW_SECONDS):
            # The refresh window is how long a replica may keep using the old credential; the
            # rotation window is how long the old one stays valid. If refresh outlives rotation the
            # two stop overlapping, and a rotation opens a gap where live replicas call authz with
            # a credential it has already stopped accepting — and authz failing closed means
            # nobody can log in, after a change that looked successful in the panel.
            raise ValueError(
                f"AUTHZ_CREDENTIAL_REFRESH_SECONDS ({self.AUTHZ_CREDENTIAL_REFRESH_SECONDS}) must "
                f"not exceed SECRETS_ROTATION_WINDOW_SECONDS "
                f"({self.SECRETS_ROTATION_WINDOW_SECONDS}) — otherwise a rotation leaves a window "
                f"where replicas authenticate with a credential authz no longer accepts")

    def assert_platform_boundary_ok(self) -> None:
        """Boot guard for the platform-capability boundary. Checked in every environment.

        The mint-time strip of '*'/'platform.*' (AuthService._safe_caps on the single-tenant path,
        tenancy.claims_for on the multi-tenant path) is only well-defined when the service knows
        which tenant IS the platform — i.e. PLATFORM_TENANT names it. With PLATFORM_TENANT empty the
        strip has no reference tenant and fails OPEN: every mint emits caps verbatim, so a stray '*'
        or 'platform.*' on any membership becomes cross-tenant authority with nothing to catch it
        (r2-05, which makes r2-01 exploitable in a stock config).

        A genuinely single-tenant deployment (no membership) is unaffected: there is one tenant, no
        customer to fence off, and '*' is meant to be usable — nothing here fires. Only the ambiguous
        case fails closed: multi-tenant membership ON and PLATFORM_TENANT UNSET, where the boundary is
        undecidable. IDENTITY_ALLOW_UNBOUNDED_PLATFORM=true is the explicit, documented opt-out for an
        operator who runs multi-tenant membership yet deliberately wants '*' usable.
        """
        if not self.IDENTITY_MULTI_TENANT:
            return
        if self.PLATFORM_TENANT:
            return
        if self.IDENTITY_ALLOW_UNBOUNDED_PLATFORM:
            return
        raise ValueError(
            "IDENTITY_MULTI_TENANT is on but PLATFORM_TENANT is empty: the platform-capability "
            "boundary is undecidable, so the mint-time strip of '*'/'platform.*' fails open and any "
            "such cap on a membership becomes cross-tenant authority (audit identity-r2-05). Set "
            "PLATFORM_TENANT to the tenant that IS the platform to enforce the boundary, or set "
            "IDENTITY_ALLOW_UNBOUNDED_PLATFORM=true to deliberately keep '*' usable in a "
            "single-tenant deployment.")

    def assert_legal_enforcement_ok(self) -> None:
        """Boot guard for legal-acceptance enforcement. Checked in every environment.

        Enforcement wired without an authority to consult is a silent no-op at best and a login
        outage at worst — say so at boot, not at 3am. `off` needs nothing.
        """
        mode = self.IDENTITY_LEGAL_ENFORCEMENT
        if mode not in ("off", "flag", "block"):
            raise ValueError(
                f"IDENTITY_LEGAL_ENFORCEMENT must be off|flag|block, not {mode!r}")
        if self.IDENTITY_LEGAL_GATE_FAIL_MODE not in ("open", "closed"):
            raise ValueError(
                f"IDENTITY_LEGAL_GATE_FAIL_MODE must be open|closed, not "
                f"{self.IDENTITY_LEGAL_GATE_FAIL_MODE!r}")
        if mode == "off":
            return
        if not self.COMPLIANCE_URL:
            raise ValueError(
                f"IDENTITY_LEGAL_ENFORCEMENT={mode} requires COMPLIANCE_URL — acceptance cannot be "
                f"enforced against an authority that is not configured")
        if not self.COMPLIANCE_INTERNAL_TOKEN:
            raise ValueError(
                f"IDENTITY_LEGAL_ENFORCEMENT={mode} requires COMPLIANCE_INTERNAL_TOKEN to call "
                f"/internal/compliance/legal/outstanding")
        if mode == "block" and not self.legal_accept_caps:
            raise ValueError(
                "IDENTITY_LEGAL_ENFORCEMENT=block requires IDENTITY_LEGAL_ACCEPT_CAPS — the "
                "restricted accept token needs at least one capability (default compliance.self) "
                "or a blocked user could never reach the accept route to unblock themselves")

    def assert_signing_rotation_ok(self) -> None:
        """Boot guard for automatic signing-key rotation. Checked in every environment.

        Rotation reseals the ring at rest and swaps the issuer for a keyring one — both have hard
        prerequisites. Enabling it without them is a misconfiguration that must stop the service,
        not degrade quietly to signing with no key or storing private keys in the clear.
        """
        if not self.TOKEN_KEY_ROTATION_ENABLED:
            return
        if self.TOKEN_ISSUER_DRIVER != "rs256":
            raise ValueError(
                f"TOKEN_KEY_ROTATION_ENABLED requires TOKEN_ISSUER_DRIVER=rs256 (asymmetric), not "
                f"{self.TOKEN_ISSUER_DRIVER!r}: a symmetric HS256 secret has no public half to "
                f"publish in a JWKS, so an overlap rotation cannot be verified by the fleet.")
        if not self.SECRETS_KEK:
            raise ValueError(
                "TOKEN_KEY_ROTATION_ENABLED requires SECRETS_KEK: the keyring holds private signing "
                "keys and is sealed at rest under the KEK. Without it there is nothing to seal with.")
        if not self.TOKEN_PRIVATE_KEY:
            raise ValueError(
                "TOKEN_KEY_ROTATION_ENABLED requires TOKEN_PRIVATE_KEY as the seed key: it is "
                "adopted as the first key in the ring on first boot and rotated automatically after.")
        overlap = self.TOKEN_SIGNING_OVERLAP_SECONDS or self.REFRESH_TTL_SECONDS + 3600
        if overlap <= self.REFRESH_TTL_SECONDS:
            # The overlap must outlive the LONGEST token (refresh), or a rotation drops the old
            # public key while refresh tokens it signed are still valid — logging those users out.
            raise ValueError(
                f"TOKEN_SIGNING_OVERLAP_SECONDS ({overlap}) must exceed REFRESH_TTL_SECONDS "
                f"({self.REFRESH_TTL_SECONDS}): a retired signing key must stay verifiable until "
                f"every token it signed — including week-long refresh tokens — has expired.")
        if self.TOKEN_KEY_ROTATION_MIN_AGE_SECONDS >= overlap:
            raise ValueError(
                f"TOKEN_KEY_ROTATION_MIN_AGE_SECONDS ({self.TOKEN_KEY_ROTATION_MIN_AGE_SECONDS}) "
                f"must be well below the overlap ({overlap}) — it only debounces a repeated cron.")

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

    @field_validator("IDENTITY_MEMBER_CAPS_POLICY")
    @classmethod
    def _known_caps_policy(cls, v: str) -> str:
        # A typo must not silently fall back — `off` misspelled as `of` selecting `subset` would keep
        # direct caps grantable on a deployment that meant roles-only. Fail at BOOT.
        from app.core.caps_grant import POLICIES
        if v not in POLICIES:
            raise ValueError(
                f"unknown IDENTITY_MEMBER_CAPS_POLICY {v!r} — allowed: {', '.join(POLICIES)}")
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the validated singleton settings.

    Raises pydantic.ValidationError at first call if the environment is invalid,
    which surfaces as a hard startup failure (fail-fast).
    """
    return Settings()  # type: ignore[call-arg]  # values are supplied by env
