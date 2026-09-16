# =============================================================================
# wallet-service — Configuration (fail-fast, ZERO-HARDCODED)
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
    SERVICE_NAME: str = "wallet-service"
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
    # Salts the subject pseudonym on an erasure ack. FROZEN — config/product.identifiers.lock.json.
    PII_SALT: str = ""

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

    # ── wallet-service (Phase 26) — zero-hardcoded ───────────────────────────
    CLOCK_DRIVER: str = "system"              # system | manual (tests)

    # THE plug-and-play knob. `name:priority:taxability` — the code never names a fund; it
    # walks them in priority order. A single-fund credits wallet is "credits:1:untaxed".
    # An `untaxed` fund applies as a PRE-VAT DISCOUNT (a gift carries no VAT); `taxed` funds
    # cover the resulting gross.
    WALLET_FUNDS: str = "promo:1:untaxed,cash:2:taxed"
    WALLET_CURRENCY: str = "SAR"
    WALLET_VAT_BP: int = 1500                 # basis points; 1500 = 15%
    # A hold that is never confirmed or released must expire, or the credits are locked
    # forever while the balance still displays them.
    HOLD_TTL_SECONDS: int = 900
    HOLD_SWEEP_LIMIT: int = 500               # bound one sweep run
    GRANT_SWEEP_LIMIT: int = 500

    # ── PER-FUND expiry policy — INFRASTRUCTURE, the platform decides each fund's mode ────────────
    # A JSON object mapping a fund name (or "*" for a default) to a policy:
    #     {"<fund>": {"ttl_days": <int>, "idle_behavior": "forfeit|dormant|extend",
    #                 "notice_days": <int>}}
    # A fund NOT listed (and no "*") = NO expiry — today's behaviour, unchanged. This service imposes
    # no policy; it provides the capability and each platform picks per fund.
    #
    # ttl_days      — how long a NEW grant stays valid, FROZEN on the grant at credit time. Changing
    #                 this later never touches grants already issued. 0 = the fund's credits never expire.
    # notice_days   — emit wallet.grant_expiring this many days before expiry (once), so a customer is
    #                 warned before anything happens. 0 = no notice.
    # idle_behavior — what happens to the UNSPENT remainder when a grant expires:
    #     forfeit — FORFEITURE (clawback): the remainder is gone, irreversibly. Legally defensible ONLY
    #               for credit that is a NON-REFUNDABLE SERVICE CREDIT — non-cashable, spendable only on
    #               a specific service (e.g. an AI-usage balance on a `locked`/non-withdrawable fund),
    #               no cash equivalent, no property right. This is the OpenAI-style service-credit model
    #               and sits OUTSIDE SAMA's prepaid stored-value regime. DO NOT use it for a fund whose
    #               balance is real money the customer could withdraw.
    #     dormant — PRESERVE + RECLAIMABLE: the remainder leaves the spendable balance (ledger `expire`)
    #               and the grant becomes state="dormant" — traceable and reversible via `reclaim`.
    #               This is the SAMA "unclaimed balances" shape: STORED VALUE (a withdrawable/cashable
    #               fund) must NOT be silently forfeited — it is held for the customer to reclaim. Use
    #               this whenever the credit represents money, or whenever the legal status is unsettled.
    #               The safe default when in doubt.
    #     extend  — auto-renew: push the grant's expiry forward by ttl_days. Credit that should not lapse
    #               while the customer is active.
    # Guidance by fund shape:
    #   • AI/usage credit on a NON-withdrawable fund, sold as non-refundable → forfeit (service credit).
    #   • Any withdrawable/cashable fund (real money) → dormant (SAMA-safe; never forfeit).
    #   • Promotional gift you never owe back → forfeit; loyalty you want to keep alive → extend.
    # (Verified against SAMA prepaid rules + gift-card/service-credit practice 2026-09-09.)
    WALLET_FUND_EXPIRY_POLICIES: str = ""
    WALLET_IDEMPOTENCY_TTL_SECONDS: int = 86_400
    # The smallest payout allowed, in MINOR units. A payout costs a bank fee, so a floor keeps a
    # 3-riyal withdrawal from costing more to send than it is worth. 0 = no minimum.
    WALLET_WITHDRAW_MIN_MINOR: int = 0
    # ── Low balance: warn BEFORE the funds run out ───────────────────────────
    # The only "you are low" without this is a 402 at spend time, which is not a warning — it is
    # an outage for whoever was mid-task.
    # 0 = OFF. A general platform opts in: a threshold guessed on an adopter's behalf is the
    # wrong number for someone, and an alert nobody asked for is one they learn to mute.
    WALLET_LOW_BALANCE_THRESHOLD: int = 0     # integer MINOR units, never a float
    # crossing  = warn ONCE, on the mutation that takes the balance under the threshold, re-arming
    #             only after it recovers
    # every_spend = the running nag, for platforms that genuinely want one
    WALLET_LOW_BALANCE_MODE: Literal["crossing", "every_spend"] = "crossing"
    # Which fund the threshold measures. Empty = the whole balance. A platform with a promo fund
    # and a cash fund usually cares about the fund that pays the bills.
    WALLET_LOW_BALANCE_FUND: str = ""
    # A broker outage must not fail a spend: the money has already moved, and the low-balance
    # state is readable on the balance response regardless. fail_closed is there for platforms
    # that would rather refuse than proceed unobserved.
    EVENT_PUBLISH_FAILURE: Literal["fail_open", "fail_closed"] = "fail_open"
    LEDGER_LIST_LIMIT: int = 100              # a statement endpoint must never dump the ledger
    # reconcile() proves sum(ledger)==balance by reading every entry seq 1..w.seq. That is unbounded
    # on a request path — a huge ledger would fire millions of sequential keyed reads inside one HTTP
    # request (a DoS reachable by any operator/internal caller). Bound the synchronous work: past this
    # many entries reconcile refuses (consistent=None, reason LEDGER_TOO_LARGE) rather than run for
    # minutes. 0 disables the bound (single-node/dev only). A platform that must verify a larger ledger
    # raises this knowing the cost, or runs the proof off the request path.
    RECONCILE_MAX_ENTRIES: int = 20_000
    # Compare-and-swap contention. Losers back off with FULL JITTER before retrying; without
    # it every loser wakes in the same instant and collides again.
    #
    # Measured against real PostgreSQL, 100 concurrent spends on ONE wallet:
    #     retries=8,  backoff=0   ->  17 succeed, 33 x HTTP 409
    #     retries=12, backoff=4ms ->  48 succeed, 52 x 409
    #     retries=20, backoff=12ms -> 100 succeed, 0 x 409
    # A 409 is always SAFE (a losing CAS writes nothing; reconcile stays consistent) — it is a
    # retry the caller should not have to make. The first attempt never sleeps, so an
    # uncontended spend is unaffected.
    CAS_MAX_RETRIES: int = 20
    CAS_BACKOFF_MS: int = 12                  # base for the full-jitter backoff; 0 = no sleep

    @property
    def wallet_funds(self) -> tuple[tuple[str, int, bool, bool], ...]:
        """[(name, priority, taxable, withdrawable)] ordered by spend priority. Fail-fast on nonsense.

        The 4th field is OPTIONAL and defaults to `locked`: a fund is spend-only unless the operator
        declares it `withdrawable`. That default is the safe one — a customer's promo credits must
        never become cash payable to a bank account by omission; only a fund an operator deliberately
        marks (an affiliate's `commission`, say) can leave the platform. So
        `promo:1:untaxed` stays locked, and `commission:2:taxed:withdrawable` can be paid out.
        """
        from app.ports.errors import ConfigError  # noqa: PLC0415 — avoid an import cycle

        out: list[tuple[str, int, bool, bool]] = []
        for chunk in self.WALLET_FUNDS.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            parts = [p.strip() for p in chunk.split(":")]
            if len(parts) not in (3, 4):
                raise ConfigError(
                    f"WALLET_FUNDS entry must be name:priority:taxability[:withdrawability] — got {chunk!r}")
            name, priority, taxability = parts[0], parts[1], parts[2]
            withdrawability = parts[3] if len(parts) == 4 else "locked"
            if not name:
                raise ConfigError("WALLET_FUNDS: empty fund name")
            if taxability not in ("taxed", "untaxed"):
                raise ConfigError(f"WALLET_FUNDS: taxability must be taxed|untaxed — got {taxability!r}")
            if withdrawability not in ("withdrawable", "locked"):
                raise ConfigError(
                    f"WALLET_FUNDS: withdrawability must be withdrawable|locked — got {withdrawability!r}")
            try:
                pri = int(priority)
            except ValueError as exc:
                raise ConfigError(f"WALLET_FUNDS: priority must be an integer — got {priority!r}") from exc
            out.append((name, pri, taxability == "taxed", withdrawability == "withdrawable"))
        if not out:
            raise ConfigError("WALLET_FUNDS must define at least one fund")
        names = [f[0] for f in out]
        if len(set(names)) != len(names):
            raise ConfigError(f"WALLET_FUNDS: duplicate fund name in {names}")
        return tuple(sorted(out, key=lambda f: f[1]))

    @property
    def fund_expiry_policies(self) -> dict[str, dict[str, Any]]:
        """Parse + VALIDATE WALLET_FUND_EXPIRY_POLICIES at boot (fail-fast: a bad money policy must
        never start). Returns {fund: {"ttl_days","idle_behavior","notice_days"}}. Each named fund must
        exist in WALLET_FUNDS ("*" is the allowed wildcard default). Empty ⇒ {} (no fund expires)."""
        from app.ports.errors import ConfigError  # noqa: PLC0415 — avoid an import cycle
        raw = (self.WALLET_FUND_EXPIRY_POLICIES or "").strip()
        if not raw:
            return {}
        import json  # noqa: PLC0415
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise ConfigError(f"WALLET_FUND_EXPIRY_POLICIES is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ConfigError("WALLET_FUND_EXPIRY_POLICIES must be a JSON object {fund: policy}")
        known = {f[0] for f in self.wallet_funds}
        out: dict[str, dict[str, Any]] = {}
        for fund, pol in parsed.items():
            if fund != "*" and fund not in known:
                raise ConfigError(f"WALLET_FUND_EXPIRY_POLICIES: unknown fund {fund!r}")
            if not isinstance(pol, dict):
                raise ConfigError(f"WALLET_FUND_EXPIRY_POLICIES[{fund!r}] must be an object")
            behavior = str(pol.get("idle_behavior", "dormant"))
            if behavior not in ("forfeit", "dormant", "extend"):
                raise ConfigError(
                    f"WALLET_FUND_EXPIRY_POLICIES[{fund!r}].idle_behavior must be "
                    f"forfeit|dormant|extend — got {behavior!r}")
            try:
                ttl = int(pol.get("ttl_days", 0)); notice = int(pol.get("notice_days", 0))
            except (TypeError, ValueError) as exc:
                raise ConfigError(
                    f"WALLET_FUND_EXPIRY_POLICIES[{fund!r}]: ttl_days/notice_days must be ints") from exc
            if ttl < 0 or notice < 0:
                raise ConfigError(f"WALLET_FUND_EXPIRY_POLICIES[{fund!r}]: days must be >= 0")
            out[fund] = {"ttl_days": ttl, "idle_behavior": behavior, "notice_days": notice}
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

    @property
    def audit_ingest_url(self) -> str:
        """Where audit events go. Empty when unconfigured — never a guessed default, because a
        wrong host would send the audit trail somewhere the operator cannot read while every call
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
