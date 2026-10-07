# =============================================================================
# payments-service — Configuration (fail-fast, ZERO-HARDCODED)
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
    SERVICE_NAME: str = "payments-service"
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

    # ── Operator-managed credentials (Phase 44) ─────────────────────────────
    # The key-encryption-key. Credentials an operator sets from the panel are sealed under this,
    # and it is settable through NO api: secrecy cannot be bootstrapped from a form, so if the key
    # protecting the secrets were itself editable in the panel, panel access would own everything.
    # Empty means the operator surface refuses to store anything rather than storing it unsealed.
    SECRETS_KEK: str = ""
    # Seals backup archives at rest. SEPARATE from SECRETS_KEK by design: restoring must not
    # require the key a running service uses for its own live secrets. Empty means this service
    # cannot produce a backup, and its export endpoint refuses rather than writing one in the clear.
    # SHARED: it must be the SAME value backup-service holds (in the class of PII_SALT /
    # INTERNAL_SERVICE_TOKEN); gen-secrets.py mints it consistently. Two different values and
    # the archives backup-service collects from this service cannot be opened.
    BACKUP_KEK: str = ""
    # The most rows a single collection may hold before a backup FAILS rather than reads on
    # unbounded. Not a limit anyone "chose" at a million — a real ceiling an operator sets to the
    # size their data actually reaches, so a runaway collection is a failed backup, not an OOM.
    BACKUP_MAX_ROWS_PER_COLLECTION: int = 5_000_000
    # How long a replaced credential stays valid. A request in flight when a key rotates must not
    # fail, and a mistaken paste needs a way back.
    SECRETS_ROTATION_WINDOW_SECONDS: int = 300

    # ── Payments conduit (Phase 4) ──────────────────────────────────────────
    # How long a minted pay-link stays usable. After this a pending request expires and the
    # redirector refuses it (fail-closed), so a stale link cannot be paid against much later.
    PAYMENTS_LINK_TTL_SECONDS: int = 259_200          # 72h
    # OUR public base URL for the token redirector. The customer is handed
    # {base}/api/v1/payments/pay/{id}, which 302s to the gateway's hosted page — so the gateway link
    # (which can leak the provider ref) is never the thing shared. Empty ⇒ hand the gateway URL
    # directly (dev only).
    PAYMENTS_REDIRECT_BASE_URL: str = ""
    # Hosts the redirector may 302 to — the shops' gateways' hosted-page hosts. FAIL-CLOSED: an empty
    # list refuses every redirect, so this is never an open redirect. JSON list or comma-separated.
    PAYMENTS_REDIRECT_ALLOWED_HOSTS: str = ""
    # The console (dev/test) gateway's fake hosted-page base. Never reached in production (a real
    # tenant selects a real driver); only the zero-infra default uses it.
    PAYMENTS_CONSOLE_LINK_BASE: str = "https://pay.example.test/checkout"
    # Per-call timeout for a shop's gateway (auth/bill/status). Kept modest — a slow gateway must not
    # hold a request open indefinitely; a timeout reads as unreachable (retry via reconcile).
    PAYMENTS_GATEWAY_TIMEOUT_SECONDS: int = 30
    # Max bytes accepted on the PUBLIC webhook. A payment status webhook is a few KB; a larger body is
    # refused (413) so an unauthenticated caller cannot pressure memory with a giant payload.
    PAYMENTS_MAX_WEBHOOK_BYTES: int = 65_536

    # ── Background reconciliation / expiry sweep (Phase 4 robustness) ─────────
    # A TuwaiqPay webhook is registered by EMAIL — the least reliable delivery there is. A dropped
    # webhook otherwise leaves money captured and the order stuck `pending` forever, because
    # reconcile/mark_expired are lazy (they only run when a redirect or an internal call happens to
    # touch the row). This sweep is the safety net: it periodically expires abandoned pendings and
    # re-queries the gateway for old ones whose webhook never arrived (reconcile is idempotent and
    # fail-closed, so a re-query can only recover a real settlement, never fabricate one).
    #
    # OFF by default: a background loop that reaches every tenant's gateway is a deliberate operational
    # choice (it spends gateway quota and needs real merchant creds wired), so nothing changes until a
    # deployment turns it on. When off, the task is never created.
    PAYMENTS_RECONCILE_SWEEP_ENABLED: bool = False
    # How often the sweep runs. Modest by default — the sweep is a safety net behind the webhook, not
    # the primary path, so it need not be aggressive; a smaller value reconciles sooner at more cost.
    PAYMENTS_RECONCILE_INTERVAL_SECONDS: int = 300      # 5 min
    # How old a PENDING request must be before the sweep re-queries its gateway. Kept well above the
    # normal webhook latency so the sweep only chases requests the webhook genuinely missed — not ones
    # still in flight (which would double the gateway load for no gain).
    PAYMENTS_RECONCILE_MIN_AGE_SECONDS: int = 900       # 15 min
    # How many rows the sweep reads per page. The sweep pages the datastore (never an unbounded scan),
    # so this bounds the working set per read regardless of how many pendings accrue.
    PAYMENTS_RECONCILE_PAGE_SIZE: int = 200
    # payments-019: max concurrent per-row gateway reconciles in a sweep page, so one slow gateway does
    # not serialise the whole pass.
    PAYMENTS_RECONCILE_CONCURRENCY: int = 8

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
    # Salts the subject pseudonym on an erasure ack. FROZEN — config/product.identifiers.lock.json.
    PII_SALT: str = ""
    ERASURE_MAX_SCAN: int = 100_000   # a DSAR is rare, but must never be an unbounded read

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

    # ── consent gate — ask before processing personal data ────────────────────
    # compliance-service served consent-check with ZERO callers for four phases: the control was
    # built and nothing consulted it. `null` answers UNKNOWN (never ALLOWED — a gate that invented
    # permission would be worse than no gate), and each call site declares what to do with that.
    CONSENT_GATE_DRIVER: str = "null"        # null | http
    COMPLIANCE_SERVICE_URL: str = ""         # e.g. http://compliance-service:8000
    # THEIR path, so it is a default rather than an assumption about another platform's routing.
    CONSENT_CHECK_PATH: str = "/internal/compliance/consent-check"
    # Short: this sits on a request path, and a slow consent question must not become a slow API.
    CONSENT_TIMEOUT_SECONDS: int = 3
    # Does this service process personal data for a declared purpose? Set by the SERVICE, not the
    # deployment — it is a fact about what the code does, and an operator should not be able to
    # switch off a control by editing an env var. It exists so the boot guard below can tell
    # "this service has no consent-gated processing" from "somebody forgot to configure the gate",
    # which are indistinguishable from the driver alone and were exactly how the gap survived.
    CONSENT_GATED: bool = False
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
    # ── Postgres connection pool ──────────────────────────────────────────────────────────────
    # asyncpg's default pool is min_size=10, max_size=10, and min is a LAZY-but-STICKY floor: the
    # pool fills to it on first use and holds there for the process lifetime. Left at the default,
    # 24 services each pin 10 backend connections — 240 against a single postgres whose stock ceiling
    # is 100 — and the fleet deadlocks on an ordinary request once enough services have been touched
    # (with maxUnavailable:0, a rollout deadlocks outright: a new pod needs its 10 slots to go Ready
    # while the old pod still holds its 10). So the floor is 1: a pod is Ready on one connection, an
    # idle service costs one, and horizontal scale spends the ceiling deliberately, not by default.
    DATASTORE_POOL_MIN: int = 1
    DATASTORE_POOL_MAX: int = 10           # load-tested ceiling; 5 exhausted under fleet-wide concurrent load while postgres sat idle (PgBouncer multiplexes these)
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

    # ── object-store credentials: one provider seam, selected by config (see credentials.py) ─────
    # static | imds | alibaba-ecs-ram | sts-endpoint. Default static = the two keys above, unchanged.
    #   imds             botocore's ambient chain (AWS EC2 / ECS task role).
    #   alibaba-ecs-ram  the Alibaba ECS instance RAM role (short-lived, self-refreshing); botocore's
    #                    imds provider cannot read Alibaba's dialect, so "" credentials there fail.
    #   sts-endpoint     a generic STS-style HTTP endpoint (JSON field names configurable below).
    OBJECTSTORE_CREDENTIALS: str = "static"
    # Metadata / STS base URL — REQUIRED for the two metadata modes (refused empty at startup). The
    # address is a property of the cloud (e.g. Alibaba serves it at http://100.100.100.200); set it in
    # the deploy values, never in code. Config allowlist, never a request value (SSRF).
    OBJECTSTORE_METADATA_ENDPOINT: str = ""
    OBJECTSTORE_METADATA_ROLE: str = ""                 # alibaba: optional — auto-discovered if empty
    OBJECTSTORE_METADATA_TOKEN_TTL: int = 21600         # alibaba IMDSv2-style metadata-token TTL (s)
    OBJECTSTORE_METADATA_TIMEOUT_SECONDS: float = 5.0   # bound the metadata call
    # sts-endpoint: which JSON field carries each value. Defaults to Alibaba's STS names; override for
    # another provider so the next cloud is a values change, not a release.
    OBJECTSTORE_STS_ACCESS_KEY_FIELD: str = "AccessKeyId"
    OBJECTSTORE_STS_SECRET_KEY_FIELD: str = "AccessKeySecret"
    OBJECTSTORE_STS_TOKEN_FIELD: str = "SecurityToken"
    OBJECTSTORE_STS_EXPIRY_FIELD: str = "Expiration"

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
    def is_production(self) -> bool:
        return self.ENV == "production"

    # Destructive/credential-writing /internal routes. Default-open is fine for reads; for these it
    # means any holder of the internal token can write a shop's payment-gateway credentials, erase a
    # person, or import a backup over live data (payments-008).
    _DESTRUCTIVE_INTERNAL_ROUTES = (
        "/internal/payments/merchant-credentials",
        # Sub-merchant onboarding/destination set a shop's PAYOUT TARGET — money-routing, same class as a
        # credential write, so they must name their caller in production (not just hold the flat token).
        "/internal/payments/sub-merchants/onboard",
        "/internal/payments/sub-merchants/destination",
        "/internal/privacy/erase",
        "/internal/backup/import",
    )

    def assert_internal_policy_ok(self) -> None:
        """Boot guard (payments-008). Refuse to start production with a destructive /internal route left
        default-open — merchant-credential writes especially must name their caller. Fail-closed on the
        declared list."""
        if not self.is_production:
            return
        import json as _json
        try:
            policy = _json.loads(self.INTERNAL_ROUTE_POLICY_JSON or "{}")
        except Exception as exc:
            raise ValueError(f"INTERNAL_ROUTE_POLICY_JSON is not valid JSON: {exc}") from exc
        covered = {str(k).rstrip("/") for k in policy}
        missing = [r for r in self._DESTRUCTIVE_INTERNAL_ROUTES
                   if not any(r == k or r.startswith(k + "/") for k in covered)]
        if missing:
            raise ValueError(
                "destructive /internal routes are default-open in production (any authenticated caller "
                f"may reach them): {', '.join(missing)}. Add each to INTERNAL_ROUTE_POLICY_JSON with a "
                'named caller list, or ["*"] to accept any authenticated caller deliberately.')

    @property
    def audit_ingest_url(self) -> str:
        """Where audit events go. Empty when no URL is configured — never a guessed default, because
        a wrong host would send the audit trail somewhere the operator cannot read while every call
        still looked successful."""
        if not self.AUDIT_SERVICE_URL:
            return ""
        return self.AUDIT_SERVICE_URL.rstrip("/") + self.AUDIT_INGEST_PATH

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

    def assert_secrets_config_ok(self) -> None:
        """Boot guard. Refuse to start production without the keys the sealed stores need.

        SECRETS_KEK seals every MERCHANT_* credential — a shop's key to its OWN money. Empty is a
        real fail-closed state (the operator surface refuses to store rather than storing unsealed),
        but for THIS service that state is useless: the whole job is minting pay-links from a shop's
        gateway, which cannot be configured at all without the KEK. Without this guard the service
        starts, passes health, and then refuses the first credential write months later — the same
        badly-timed failure the PII guard prevents. A misconfiguration must refuse to START.

        BACKUP_KEK seals the archives backup-service collects from this service; the export surface is
        always mounted, so an empty key means every export 503s and this service silently drops out of
        the platform's backups. Both keys are minted together by gen-secrets.py, so in a real
        production deploy both are always present — an empty one is a provisioning slip, caught here.
        """
        if not self.is_production:
            return
        if not self.SECRETS_KEK:
            raise ValueError(
                "SECRETS_KEK is empty in production: merchant gateway credentials could never be "
                "sealed or read, so this service could take no payments at all, and the failure "
                "would surface only on the first credential op. Set SECRETS_KEK (gen-secrets.py "
                "mints it; it is settable through NO api by design)."
            )
        if not self.BACKUP_KEK:
            raise ValueError(
                "BACKUP_KEK is empty in production: the export surface is mounted but every backup "
                "would 503, so this service would silently drop out of the platform's backups. Set "
                "BACKUP_KEK to the SAME value backup-service holds (gen-secrets.py mints it "
                "consistently — see config/product.identifiers.lock.json)."
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
