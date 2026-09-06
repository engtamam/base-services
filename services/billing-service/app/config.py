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
    DATASTORE_POOL_MAX: int = 5
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

    # ── billing-service (Phase 14) — all config-driven, zero-hardcoded ─────
    PAYMENT_GATEWAY_DRIVER: str = "null"     # null|stripe|moyasar|tap|hyperpay|paytabs|geidea|aps|paypal|checkout|tuwaiqpay
    # How a renewal chooses its gateway when the active driver is switched — a ports/adapters MODE so
    # each deployment picks (zero-hardcoded):
    #   active (default, today's behaviour): renewals charge the CURRENTLY-active gateway. Switching
    #          the driver breaks existing subscribers whose saved token belongs to the old gateway —
    #          use the activation preview to see how many before switching.
    #   pinned: each subscription remembers the gateway it was created on (Subscription.payment_gateway)
    #          and its renewals/refunds route THERE, not to whatever is active now; switching the driver
    #          only affects NEW subscriptions; a subscriber moves only when they update their payment
    #          method. Parallel-safe, no manual transition. A pinned gateway that cannot build fails
    #          CLOSED (retryable by dunning) — it never silently charges a different gateway.
    PAYMENT_RENEWAL_GATEWAY_MODE: str = "active"    # active | pinned
    PAYMENT_API_KEY: str = ""                # gateway secret/API key (Bearer/Basic)
    PAYMENT_API_BASE: str = ""              # override gateway base URL (host egress-allowlisted)
    PAYMENT_EXTRA_ID: str = ""               # HyperPay entityId / PayTabs profile_id / APS merchant_identifier
    PAYMENT_EXTRA_SECRET: str = ""           # Geidea api-password / APS SHA request phrase
    PAYMENT_WEBHOOK_SECRET: str = ""         # webhook signature secret (APS: SHA response phrase)
    # Where the payer is returned after a redirect/3DS step. Moyasar REQUIRES a callback_url for a
    # token/card source; redirect-flow providers use it as the return target. Empty ⇒ not sent.
    PAYMENT_RETURN_URL: str = ""
    # Max age of a signed webhook whose scheme carries a timestamp (Stripe's t=…): an event older
    # than this is rejected as a replay even if the signature is valid. Stripe's own default is 300s.
    # 0 disables the freshness check (signature only).
    PAYMENT_WEBHOOK_TOLERANCE_SECONDS: int = 300
    # Which money document a paid invoice produces — an OPERATOR CHOICE, per tenant, at runtime.
    #   receipt      (DEFAULT) our own internal receipt (سند قبض): no VAT, no ZATCA, no seller VAT
    #                number — it just confirms money received and carries the gateway's transaction
    #                reference. Every platform can issue it; a fresh one needs nothing configured.
    #   tax_invoice  the ZATCA tax invoice (e-invoiced + VAT + QR). Requires the einvoicer/PDF drivers.
    #   both         issue both.
    # Read at the point of use (_mark_paid + the document routes); the ZATCA e-invoice is finalized
    # ONLY for tax_invoice/both, so a receipt-only platform never touches e-invoicing.
    BILLING_DOCUMENT_MODE: str = "receipt"   # receipt | tax_invoice | both

    TAX_CALCULATOR_DRIVER: str = "vat"       # vat | null
    VAT_RATE: str = "0.15"                    # KSA standard rate
    REVERSE_CHARGE_B2B: bool = False          # B2B w/ buyer VAT → 0-rate (cross-border reverse charge)
    EINVOICER_DRIVER: str = "null"           # null | zatca-http
    ZATCA_SERVICE_URL: str = ""              # base URL of zatca-service (/internal/zatca/finalize)
    ZATCA_SERVICE_TOKEN: str = ""            # X-Internal-Token for zatca-service
    PDF_RENDERER_DRIVER: str = "null"        # null | reportlab
    # The Arabic-capable face embedded in the invoice PDF. Empty uses the bundled Droid Sans
    # Arabic (Apache-2.0, © The Android Open Source Project — see the NOTICE beside it). Point
    # this at a corporate typeface to brand the document without patching code.
    INVOICE_FONT_PATH: str = ""
    # How many line items a rendered invoice shows. The document is built in memory, so an invoice
    # with a hundred thousand lines is an upstream bug that must not take the service down; the
    # PDF says plainly that it was truncated rather than quietly showing a partial total.
    INVOICE_MAX_LINES: int = 500
    # Printed as the supplier on every invoice. ZATCA requires the seller's name and VAT number on
    # a tax invoice, so leaving these empty produces a document that is not compliant — they are
    # deliberately not defaulted to a placeholder that would look filled in.
    INVOICE_SELLER_NAME: str = ""
    INVOICE_SELLER_VAT: str = ""
    INVOICE_SELLER_CR: str = ""
    INVOICE_SELLER_ADDRESS: str = ""
    CURRENCY: str = "SAR"
    TRIAL_DAYS: int = 14
    GRANDFATHER: bool = True                  # existing subs keep signup price/limits until migrated
    DUNNING_RETRY_BASE_HOURS: int = 24
    # ── renewal sweep ─────────────────────────────────────────────────────────
    # The most subscriptions one cycle-due pass will invoice. A ceiling an operator sets to their
    # real scale, so a runaway sweep is bounded and the daily cron finishes; the report says when
    # it was capped so the next run picks up the rest.
    # The most keyset pages the platform invoice view scans while applying a since/until window.
    # A ceiling, because the datastore cannot range on a date: a very wide window would otherwise
    # walk the whole ledger. When it caps, next_cursor is still returned so the caller can continue.
    PLATFORM_MAX_PAGES: int = 50
    CYCLE_SWEEP_LIMIT: int = 1000
    # Invoice this many seconds BEFORE a period actually ends, so a renewal is raised before access
    # lapses. 0 = exactly at the boundary. Never negative — that would skip due renewals.
    CYCLE_LEAD_SECONDS: int = 0
    DUNNING_MAX_ATTEMPTS: int = 4
    GRACE_DAYS: int = 7
    # When a paused subscription's invoice is paid (card, webhook or off-gateway settle), reactivate
    # it automatically — the standard reopens the instant the money lands, no manual reactivate.
    # Runtime-editable per tenant (RUNTIME_POLICY); read at the point of use in _mark_paid.
    BILLING_AUTO_REACTIVATE: bool = True

    # ── grant clawback on refund / chargeback (Phase 51) ──────────────────────
    # When a paid invoice granted wallet credits and is later refunded or charged back, reverse the
    # grant so the customer does not keep both the money and the credits. Every line here is an
    # operator CHOICE — how a business handles a refunded credit is policy, not law:
    #   ENABLED         master switch; runtime-editable per tenant (RUNTIME_POLICY), read at use.
    #   ALLOW_NEGATIVE  let the wallet go below zero when the credits are already spent — otherwise
    #                   the reversal is refused (grant.clawback_failed) and the customer keeps them.
    #   ON_PARTIAL      reverse the grant on a PARTIAL refund too (off: only a full void reverses).
    #   SKIP_EXPIRED    do not reverse a `reset` grant that already lapsed — the credits are gone.
    BILLING_CLAWBACK_ENABLED: bool = True
    BILLING_CLAWBACK_ALLOW_NEGATIVE: bool = True
    BILLING_CLAWBACK_ON_PARTIAL: bool = False
    BILLING_CLAWBACK_SKIP_EXPIRED: bool = True

    # ── entitlement resolution (Phase 42) ─────────────────────────────────────
    # A tenant's entitlements are the UNION of everything they pay for. Resolution used to read
    # ONE subscription, so a customer who bought three things could use one — and which one
    # depended on insertion order. The refusal was a fail-closed 402, indistinguishable from
    # "you have not paid for this".
    #
    # WHICH SUBSCRIPTIONS COUNT is a credit decision, not a technical one, so it is config.
    # `canceled` is deliberately absent and cannot be added: a cancelled subscription that still
    # granted access would make cancellation a suggestion. past_due counts only inside GRACE_DAYS.
    BILLABLE_STATUSES: str = "active,trialing,past_due"
    # Bound on how many subscriptions one tenant may resolve from. A ceiling, not a page size:
    # reaching it means something unusual, and silently resolving from a truncated set is how a
    # paying customer loses an entitlement.
    MAX_BILLABLE_SUBSCRIPTIONS: int = 200
    # How to combine one key across subscriptions, per KEY. A global sum-or-max switch breaks on
    # the first mixed catalogue: seats SUM, retention MAX, sso OR, support_tier RANK — which is
    # neither sum nor max. Unlisted keys default BY TYPE (numbers sum, booleans OR).
    #   {"seats": "sum", "max_retention_days": "max", "support_tier": "rank"}
    ENTITLEMENT_MERGE_STRATEGIES: str = ""
    # Ordered vocabularies for RANK keys. Without one a rank key REFUSES rather than inventing an
    # order — an invented order silently promotes or demotes a customer's support tier.
    #   {"support_tier": ["bronze", "silver", "gold"]}
    ENTITLEMENT_RANK_ORDERS: str = ""
    # Entitlements FAIL CLOSED on a lapsed period. An active/trialing subscription whose
    # current_period_end has passed stops contributing — because the renewal sweep, not the resolver,
    # is what moves a paid period forward, and if the sweep is paused or errors, an entitlement that
    # ignored the period end would keep granting full access with nothing red anywhere. A missed
    # sweep must look like an outage, not generosity. This tolerance is how long PAST the period end a
    # subscription still counts, so a sweep running a little late (CYCLE_LEAD_SECONDS already invoices
    # BEFORE the boundary) never cuts anyone off. A STATED tolerance is defensible; the unbounded one
    # that existed because the field was not read is not. Default one day. past_due uses GRACE_DAYS
    # instead — a late payment is a longer, credit-decision window.
    ENTITLEMENT_PERIOD_TOLERANCE_SECONDS: int = 86_400

    @property
    def billable_statuses(self) -> tuple[str, ...]:
        return tuple(s.strip() for s in self.BILLABLE_STATUSES.split(",") if s.strip())

    @property
    def entitlement_merge_strategies(self) -> dict[str, str]:
        import json as _json
        raw = (self.ENTITLEMENT_MERGE_STRATEGIES or "").strip()
        if not raw:
            return {}
        try:
            parsed = _json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"ENTITLEMENT_MERGE_STRATEGIES is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()):
            raise ValueError('ENTITLEMENT_MERGE_STRATEGIES must be {"key": "strategy"}')
        from app.core.entitlement_merge import STRATEGIES
        bad = {k: v for k, v in parsed.items() if v not in STRATEGIES}
        if bad:
            # At BOOT, not at the first entitlement check. A typo here silently refuses a key for
            # every tenant, and a refusal looks exactly like "you did not pay for this".
            raise ValueError(f"unknown merge strategy in {bad!r}; one of {', '.join(STRATEGIES)}")
        return parsed

    @property
    def entitlement_rank_orders(self) -> dict[str, list[str]]:
        import json as _json
        raw = (self.ENTITLEMENT_RANK_ORDERS or "").strip()
        if not raw:
            return {}
        try:
            parsed = _json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"ENTITLEMENT_RANK_ORDERS is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict) or not all(
                isinstance(v, list) and all(isinstance(x, str) for x in v)
                for v in parsed.values()):
            raise ValueError('ENTITLEMENT_RANK_ORDERS must be {"key": ["low", …, "high"]}')
        return parsed
    BILLING_LIST_LIMIT: int = 100
    # The most paid/voided invoices GET /internal/billing/invoices returns for one tenant — a bounded
    # read (never a scan) for a peer service keying money on the history. Generous (a decade of monthly
    # invoices is ~120), and a truncated flag makes the pathological case loud rather than silent.
    INTERNAL_INVOICE_HISTORY_MAX: int = 1000

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

    # Salts the subject pseudonym on a retained tax invoice. FROZEN — see the identifier lock.
    PII_SALT: str = ""

    # ── operator-managed credentials ──────────────────────────────────────────
    # Seals the payment credentials an operator sets from the panel. Never settable through any
    # API: a key that protects the secrets and is itself editable in the UI protects nothing from
    # someone who has the UI. Empty means the operator surface refuses rather than storing in the
    # clear.
    # ── wallet grants: the durable path from "they paid" to "they got it" ─────
    # OFF by default. An adopter who does not run wallet-service must not acquire a background
    # task that fails for ever against a service they never deployed. Recording still happens when
    # this is off, so switching it on later delivers everything that accumulated.
    WALLET_DRIVER: str = "null"                  # null | http
    WALLET_SERVICE_URL: str = ""                 # host must be in EGRESS_ALLOWED_HOSTS
    WALLET_SERVICE_TOKEN: str = ""               # operator-manageable; sealed in the SecretStore
    WALLET_TIMEOUT_SECONDS: int = 10
    # Which wallet fund a plan's included credits land in.
    WALLET_GRANT_FUND: str = "credits"
    # Where the grant amount comes from on a plan: plan.limits[<this key>], in minor units.
    PLAN_CREDIT_LIMIT_KEY: str = "credits"
    # Do unused included credits carry over at renewal, or start fresh?
    PLAN_CREDIT_GRANT_MODE: str = "accumulate"   # accumulate | reset
    # Retry backoff for an undelivered grant. There is no attempt CAP on purpose: giving up would
    # silently keep money a customer paid for.
    GRANT_RETRY_BASE_SECONDS: int = 30
    GRANT_RETRY_MAX_SECONDS: int = 3600
    GRANT_DRAIN_LIMIT: int = 100
    GRANT_RECONCILE_LIMIT: int = 200

    # ── domain events ─────────────────────────────────────────────────────────
    # What happens when the event bus cannot be reached. The money has already moved by the time
    # these publish, so refusing to record a payment because a broker is unreachable turns a
    # degraded notification path into a failed payment. fail_closed exists for platforms that
    # would rather refuse than proceed unobserved; it is not the default, because here the
    # alternative to a missed hint is a lost payment.
    EVENT_PUBLISH_FAILURE: str = "fail_open"      # fail_open | fail_closed

    SECRETS_KEK: str = ""
    # Seals backup archives at rest. SEPARATE from SECRETS_KEK: restoring must not require the key
    # this service uses for its own live secrets. Empty means the backup endpoint refuses rather
    # than writing an archive in the clear.
    # SHARED: it must be the SAME value backup-service holds (in the class of PII_SALT /
    # INTERNAL_SERVICE_TOKEN); gen-secrets.py mints it consistently. Two different values and
    # the archives backup-service collects from this service cannot be opened.
    BACKUP_KEK: str = ""
    # The most rows a single collection may hold before a backup FAILS rather than reads on
    # unbounded. Not a limit anyone "chose" at a million — a real ceiling an operator sets to the
    # size their data actually reaches, so a runaway collection is a failed backup, not an OOM.
    BACKUP_MAX_ROWS_PER_COLLECTION: int = 5_000_000
    SECRETS_ROTATION_WINDOW_SECONDS: int = 300
    # How long resolved gateway credentials are reused before being read again. Resolving on every
    # charge would put a datastore read and a scrypt-backed decrypt on the payment path.
    PROVIDER_REFRESH_SECONDS: int = 30

    # How long a tax invoice must survive. ZATCA/KSA is 6; several EU states are 10. It is a
    # PLATFORM/jurisdiction decision, never ours — so it is config, and it is quoted back to the
    # person in the erasure receipt rather than asserted in a docstring nobody reads.
    INVOICE_RETENTION_YEARS: int = 10

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

    @property
    def audit_ingest_url(self) -> str:
        """Where audit events go. Empty when unconfigured — never a guessed default, because a
        wrong host would send the audit trail somewhere the operator cannot read while every call
        still looked successful."""
        if not self.AUDIT_SERVICE_URL:
            return ""
        return self.AUDIT_SERVICE_URL.rstrip("/") + self.AUDIT_INGEST_PATH

    def assert_secrets_config_ok(self) -> None:
        """Boot guard: the refresh window must not outlive the rotation window.

        The refresh window is how long a replica keeps using a credential it already resolved; the
        rotation window is how long the previous one stays valid. If refresh outlives rotation the
        two stop overlapping, and a rotation opens a gap where live replicas charge with a key the
        gateway has already stopped accepting — every payment failing, after a change that looked
        successful in the panel.
        """
        if self.SECRETS_KEK and (
                self.PROVIDER_REFRESH_SECONDS > self.SECRETS_ROTATION_WINDOW_SECONDS):
            raise ValueError(
                f"PROVIDER_REFRESH_SECONDS ({self.PROVIDER_REFRESH_SECONDS}) must not exceed "
                f"SECRETS_ROTATION_WINDOW_SECONDS ({self.SECRETS_ROTATION_WINDOW_SECONDS}) — "
                f"otherwise a rotation leaves a window where charges use a key the gateway no "
                f"longer accepts")

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

    @field_validator("BILLING_DOCUMENT_MODE")
    @classmethod
    def _valid_document_mode(cls, v: str) -> str:
        allowed = {"receipt", "tax_invoice", "both"}
        if v not in allowed:
            raise ValueError(f"BILLING_DOCUMENT_MODE must be one of {sorted(allowed)}, got {v!r}")
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the validated singleton settings.

    Raises pydantic.ValidationError at first call if the environment is invalid,
    which surfaces as a hard startup failure (fail-fast).
    """
    return Settings()  # type: ignore[call-arg]  # values are supplied by env
