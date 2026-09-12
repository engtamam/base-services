# =============================================================================
# assistant-service — Configuration (fail-fast, ZERO-HARDCODED)
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
    SERVICE_NAME: str = "assistant-service"
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

    # ── Knowledge base — where the assistant's facts come from ──────────────
    # NEVER the request body. Whoever controls the knowledge controls what the product appears to
    # assert, so the source is operator-configured and a caller may only say what it is asking.
    # `composite` merges several in order, and the FIRST source wins on a duplicate id — put the
    # authored file first so a human can override one generated sentence without forking anything.
    KB_SOURCE_DRIVER: str = "memory"          # memory | file | catalog | composite
    KB_FILE_PATH: str = ""                    # a .json file, or a directory of them
    KB_CATALOG_URL: str = ""                  # an aggregated action catalog
    KB_CATALOG_TIMEOUT_SECONDS: float = 10.0
    KB_CATALOG_PREFIX: str = "action"         # namespace for generated ids
    # Which sources compose, in priority order. Only read when the driver is `composite`.
    KB_COMPOSITE_SOURCES: str = "file,catalog"

    # ── Retrieval ───────────────────────────────────────────────────────────
    # Chunk size is about CITATION, not cost: a citation a reader cannot check is not a citation.
    KB_CHUNK_MAX_CHARS: int = 900
    # How many passages an answer may draw on. More stops being an answer and becomes a search page.
    RETRIEVAL_TOP_N: int = 6
    # The precision lever. Below this a passage is not evidence, and the assistant abstains rather
    # than reaching for the least-bad match — which is how confident wrong answers are produced.
    RETRIEVAL_MIN_SCORE: float = 0.35
    # The context budget. This is what keeps prompt size independent of KB size; without it the
    # bill and the latency track how much help has been written.
    RETRIEVAL_MAX_CONTEXT_CHARS: int = 6000
    # BM25 tuning. Surfaced because the right values depend on a corpus we do not have yet.
    RETRIEVAL_BM25_K1: float = 1.2
    RETRIEVAL_BM25_B: float = 0.75

    # ── Semantic scoring (optional) ─────────────────────────────────────────
    # Off by default: embeddings need either a model in-process (a large dependency and image) or
    # a network call per query and per entry (a key, a bill, a failure mode). Neither belongs in
    # the path a service takes to boot with nothing configured. Lexical answers on its own.
    EMBEDDINGS_DRIVER: str = "null"           # null | http
    EMBEDDINGS_URL: str = ""
    EMBEDDINGS_MODEL: str = ""
    EMBEDDINGS_DIMENSIONS: int = 0
    EMBEDDINGS_TIMEOUT_SECONDS: float = 10.0
    # Blend weight: 1.0 = purely lexical, 0.0 = purely semantic. Lexical is exact and always finds
    # a product name or an error code; semantic is fuzzy in both directions. The default leans
    # lexical because being findable by the exact word is the property users notice losing.
    RETRIEVAL_HYBRID_ALPHA: float = 0.65

    # ── Answering ───────────────────────────────────────────────────────────
    # `extractive` needs no API key and cannot hallucinate: it quotes the retrieved passages. It is
    # the default because a help assistant that requires a key before it does anything is a feature
    # an adopter evaluates later and never switches on.
    # SSRF guard for tenant-supplied AI-connection URLs (the service POSTs a credential to them).
    # https required by default; private/link-local/metadata addresses blocked unless a platform opts
    # in (single-node/dev). allow/deny host lists are CSV. "كل منصة تختار."
    ASSISTANT_CONN_REQUIRE_HTTPS: bool = True
    ASSISTANT_CONN_ALLOW_PRIVATE_IPS: bool = False
    ASSISTANT_CONN_ALLOWED_HOSTS: str = ""    # CSV allowlist; empty = any public host
    ASSISTANT_CONN_DENIED_HOSTS: str = ""     # CSV denylist

    MODEL_DRIVER: str = "extractive"          # extractive | http
    # One adapter, several vendors, no SDK: the wire format is a JSON POST returning a string, and
    # a vendor SDK per provider is a large amount of supply chain for a small convenience.
    MODEL_URL: str = ""
    MODEL_API_KEY: str = ""
    MODEL_NAME: str = ""
    # How the http model authenticates:
    #   api_key (default) — the static MODEL_API_KEY, sent as a Bearer (OpenAI/OpenRouter and any
    #                       compatible endpoint).
    #   vertex            — Vertex AI (GCP): a SHORT-LIVED OAuth token minted + cached per call, for
    #                       KSA data-residency (me-central2/Dammam). Point MODEL_URL at the regional
    #                       endpoint and MODEL_SHAPE=generate. Credential source below.
    MODEL_AUTH: str = "api_key"              # api_key | vertex
    # Vertex service-account key JSON (a SECRET). Empty in `vertex` mode ⇒ the GCP metadata server
    # (Workload Identity) is used instead — the no-key posture when running on GCP.
    MODEL_GCP_SA_JSON: str = ""
    MODEL_GCP_SCOPE: str = "https://www.googleapis.com/auth/cloud-platform"
    # Prompt caching (cost). Gemini + OpenAI cache the stable prefix implicitly (free — this adapter
    # already sends the constant system block first, the variable question last), so nothing is needed
    # for them. `ephemeral` adds Anthropic's explicit cache_control marker (its 90% cached-read
    # discount requires the opt-in) — set it ONLY for an Anthropic-style endpoint. off = today.
    MODEL_PROMPT_CACHE: str = "off"          # off | ephemeral
    # Optional pre-created Vertex/Gemini context cache resource name, passed as `cachedContent`.
    MODEL_GEMINI_CACHED_CONTENT: str = ""
    # CONFIGURED, never auto-detected. Guessing a response shape means an unexpected payload reads
    # as "no answer", which surfaces as an abstention and sends someone hunting a thin knowledge
    # base when the real fault is a schema mismatch.
    #   chat      messages[] -> choices[].message.content   (OpenAI-compatible: OpenAI, Qwen,
    #             DeepSeek, MiniMax, Zhipu/GLM/ZAI, OpenRouter — and Vertex Model Garden's
    #             .../endpoints/openapi/chat/completions route for Llama etc.)
    #   generate  contents[] -> candidates[]                (Gemini shape, incl. Gemini on Vertex)
    #   anthropic messages[] + top-level system -> content[].text   (Claude — direct API, OR
    #             Claude-on-Vertex :rawPredict when MODEL_ANTHROPIC_VERSION is set)
    MODEL_SHAPE: str = "chat"                 # chat | generate | anthropic
    # ANTHROPIC shape only. Set to Vertex's body version (e.g. "vertex-2023-10-16") to call
    # Claude-on-Vertex publishers/anthropic/...:rawPredict (model named in MODEL_URL, Bearer GCP token).
    # Leave empty for the direct Anthropic API (model in the body, x-api-key + anthropic-version header).
    MODEL_ANTHROPIC_VERSION: str = ""
    MODEL_TEMPERATURE: float = 0.2
    MODEL_MAX_ANSWER_TOKENS: int = 700
    # Arrived with the provider adapter, which is when it started meaning something.
    MODEL_TIMEOUT_SECONDS: float = 20.0
    MODEL_EXTRACTIVE_MAX_PASSAGES: int = 3

    # ── Conversations & privacy ─────────────────────────────────────────────
    # A help question is user content and routinely carries the identifiers the platform works to
    # keep out of its logs — people paste card numbers into help boxes when a payment fails.
    # Redaction is ON by default: the deployment that most needs it is the one that never read
    # this setting.
    REDACT_PII: bool = True
    REDACT_MAX_CHARS: int = 20_000
    # Vertex AI "priority" tier (Priority PayGo): a per-request header buys more consistent latency
    # during congestion at ~1.8x the standard per-token rate. LOCKED by default — the capability
    # exists but is inert until the PLATFORM turns this on, so nobody can incur the surcharge by
    # accident. Even when on, it applies only to a Vertex connection AND only when the caller sets
    # `priority:true` on that specific invoke (both locks must be open). Off ⇒ today's behaviour.
    ASSISTANT_PRIORITY_TIER_ENABLED: bool = False
    # Storing history is opt-in: a service that keeps nothing has nothing to leak or erase.
    CONVERSATIONS_ENABLED: bool = False
    # Refuses to start rather than store plaintext when on without a key.
    ENCRYPT_MESSAGES: bool = True
    MESSAGE_KEY: str = ""
    CONVERSATION_PAGE_SIZE: int = 20
    CONVERSATION_RETENTION_DAYS: int = 90     # 0 = keep until erased

    # ── Daily quota — the spend ceiling, distinct from the burst limiter ─────
    # A rate limiter stops hammering in a minute; it does nothing about a user who asks a
    # reasonable question every thirty seconds all day. That is the one that reaches an invoice.
    # TWO ceilings: a single tenant-wide counter lets the first enthusiastic user exhaust the day
    # for every colleague, and the quota then reads as an outage to people who did nothing wrong.
    # 0 = no ceiling.
    QUOTA_DAILY_PER_SUBJECT: int = 200
    QUOTA_DAILY_PER_TENANT: int = 5_000

    # ── Branding (the widget's look) ────────────────────────────────────────
    # Deployment defaults. A tenant or a surface may refine them; every value is validated as a
    # strict hex colour, an http(s) URL or plain text before it is stored or served, because
    # branding is stored, served to every user of a tenant, and nobody thinks of it as untrusted.
    BRAND_NAME: str = "Assistant"
    BRAND_GREETING: str = ""
    BRAND_PRIMARY_COLOR: str = "#2563eb"
    BRAND_SURFACE_COLOR: str = "#ffffff"
    BRAND_TEXT_COLOR: str = "#111827"
    BRAND_LOGO_URL: str = ""
    BRAND_POSITION: str = "bottom-right"
    # Per-tenant and per-surface overrides, as JSON: {"tenant": {...}} / {"tenant:surface": {...}}
    BRAND_OVERRIDES: str = "{}"
    # Serving the widget is opt-in: a deployment that embeds its own UI should not also publish a
    # script it does not use.
    WIDGET_ENABLED: bool = True

    # The language used when a caller does not ask for one.
    DEFAULT_LANGUAGE: str = "en"
    # What the assistant says when the evidence is too thin. Deliberately configurable per
    # deployment: the abstention is the sentence users see most when a KB is young, and it should
    # sound like the product rather than like a library.
    # A model outage is NOT a knowledge gap, and must not wear its words: one asks the reader to
    # write help content, the other to wait a moment.
    ASSISTANT_UNAVAILABLE_TEXT: str = (
        "The assistant is temporarily unavailable. Please try again in a moment."
    )
    ASSISTANT_NOT_FOUND_TEXT: str = (
        "I don't have anything on that yet. Try rephrasing, or ask your administrator to add it "
        "to the help content."
    )

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

    # Salts the subject pseudonym on an erasure ack. FROZEN — config/product.identifiers.lock.json.
    PII_SALT: str = ""
    ERASURE_MAX_SCAN: int = 100_000   # a DSAR is rare, but must never be an unbounded read

    # ── operator-managed credentials ─────────────────────────────────────────
    # Seals credentials an operator sets from the panel. Never settable through any API: a key
    # that protects the secrets and is itself editable in the UI protects nothing from someone
    # who has the UI. Empty means the operator surface refuses rather than storing in the clear.
    SECRETS_KEK: str = ""
    SECRETS_ROTATION_WINDOW_SECONDS: int = 300
    # How long a resolved model credential is reused before the store is read again. Resolving it
    # per question would put a datastore read and a scrypt-backed decrypt on the answering hot
    # path, to support a change that happens a few times a year.
    MODEL_CREDENTIAL_REFRESH_SECONDS: int = 30

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

    def assert_secrets_config_ok(self) -> None:
        """Boot guard. The credential cache must not outlive the rotation it depends on.

        The refresh window is how long a replica may keep using the credential it already
        resolved; the rotation window is how long the previous one stays acceptable. If refresh
        outlives rotation the two stop overlapping, and a rotation opens a gap where live replicas
        still call the provider with a key it has already stopped accepting — every answer failing,
        after a change that looked successful in the panel.
        """
        if self.SECRETS_KEK and (
                self.MODEL_CREDENTIAL_REFRESH_SECONDS > self.SECRETS_ROTATION_WINDOW_SECONDS):
            raise ValueError(
                f"MODEL_CREDENTIAL_REFRESH_SECONDS ({self.MODEL_CREDENTIAL_REFRESH_SECONDS}) must "
                f"not exceed SECRETS_ROTATION_WINDOW_SECONDS "
                f"({self.SECRETS_ROTATION_WINDOW_SECONDS}) — otherwise a rotation leaves a window "
                f"where replicas call the model with a credential it no longer accepts")

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
