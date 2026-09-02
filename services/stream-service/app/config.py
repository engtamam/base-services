# =============================================================================
# stream-service — Configuration (fail-fast, ZERO-HARDCODED)
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
    SERVICE_NAME: str = "stream-service"
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

    # Salts the subject pseudonym on an erasure ack. FROZEN — config/product.identifiers.lock.json.
    PII_SALT: str = ""
    ERASURE_MAX_SCAN: int = 100_000   # a DSAR is rare, but must never be an unbounded read


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

    # ── stream-service (Phase 35) — all config-driven ──────────────────────────
    # Drivers (every capability is a port; a null adapter lets the service boot with zero infra).
    STREAM_ENGINE_DRIVER: Literal["ffmpeg", "null"] = "null"
    SESSION_PLACER_DRIVER: Literal["local", "worker-pool"] = "local"
    PTZ_DRIVER: Literal["onvif", "null"] = "null"
    MOTION_DRIVER: Literal["ffmpeg-scene", "none"] = "none"
    DISCOVERY_DRIVER: Literal["onvif-ws", "none"] = "none"
    RECORDING_SINK_DRIVER: Literal["file-service", "s3", "local", "memory"] = "file-service"
    MEDIA_EGRESS_DRIVER: Literal["direct", "turn"] = "direct"
    SIGNED_PLAYBACK_DRIVER: Literal["hmac"] = "hmac"
    FFMPEG_PATH: str = "ffmpeg"

    # Sessions & auto-stop guards (a session can NEVER run forever — the forgot-to-stop safety net).
    SESSION_DEFAULT_MODE: Literal["on_demand", "viewer_driven", "continuous", "scheduled"] = "on_demand"
    SESSION_DEFAULT_OUTPUTS: str = "hls"          # comma list of hls|webrtc|snapshot|thumbnail|record
    SESSION_MAX_DURATION_SECONDS: int = 14_400    # 4h hard cap; per-session guard clamps to this
    SESSION_IDLE_TIMEOUT_SECONDS: int = 300       # viewer_driven/on_demand: no viewer -> stop
    SESSION_MAX_SIZE_BYTES: int = 10_737_418_240  # 10 GiB recorded -> stop
    SESSION_LEASE_SECONDS: int = 30               # heartbeat deadline; missed -> reaped as a zombie
    SESSION_START_TIMEOUT_SECONDS: int = 20       # no first frame within this -> error, kill process
    VIEWER_GRACE_SECONDS: int = 30                # last viewer left -> wait this long before stopping
    ONE_SESSION_PER_SOURCE: bool = False
    SCHEDULE_TIMEZONE_DEFAULT: str = "UTC"

    # Outputs.
    HLS_SEGMENT_SECONDS: int = 2
    HLS_WINDOW_SEGMENTS: int = 5
    HLS_LOW_LATENCY: bool = False
    WEBRTC_ENABLED: bool = False
    WEBRTC_MAX_VIEWERS_PER_SESSION: int = 25
    # The media server the engine WHIP-publishes to and the browser WHEP-subscribes from (e.g.
    # http://mediamtx:8889). Media flows engine → this server → browser, never through the app.
    WEBRTC_MEDIA_BASE_URL: str = ""
    TURN_URLS: str = ""                           # comma list; media egress for WebRTC across NAT
    TURN_SECRET: str = ""                         # sealed
    SNAPSHOT_MIN_INTERVAL_SECONDS: int = 1
    THUMBNAIL_INTERVAL_SECONDS: int = 60
    SNAPSHOT_MAX_WIDTH: int = 1280
    RECORD_SEGMENT_SECONDS: int = 60
    RECORD_FORMAT: Literal["fmp4", "mp4", "mkv"] = "fmp4"
    RECORD_ON_START: bool = False
    RECORDING_FILE_SERVICE_URL: str = ""          # where segments are stored (file-service base url)
    RECORDING_FILE_SERVICE_TOKEN: str = ""        # a service JWT with files.upload for the sink to post
    TRANSCODE_PROFILES: str = ""                  # JSON: {"id": {"scale":"1280:-1","vbitrate":"2M",...}}

    # ── operator-managed credentials ──────────────────────────────────────────
    # Seals credentials an operator sets from the panel. Never settable through any API: a key
    # that protects the secrets and is itself editable in the UI protects nothing from someone
    # who has the UI. Empty means the operator surface refuses rather than storing in the clear.
    SECRETS_KEK: str = ""
    SECRETS_ROTATION_WINDOW_SECONDS: int = 300
    # How long a resolved sink credential is reused before it is read again. A recording sink
    # authenticates on every segment — one datastore read and one decrypt per 60-second segment,
    # per session, to support a change that happens a few times a year.
    RECORDING_CREDENTIAL_REFRESH_SECONDS: int = 30

    # Retention & export.
    RETENTION_DAYS: int = 30
    RETENTION_MAX_BYTES: int = 0                  # 0 = unlimited (age-based only)
    # ── clip export ───────────────────────────────────────────────────────────
    # `null` produces nothing and SAYS so — never a fake `ready`, never a permanent `pending`.
    # The boot guard refuses it in production when EXPORT_ENABLED is on.
    EXPORT_DRIVER: str = "null"                   # null | ffmpeg
    EXPORT_ENABLED: bool = False                  # does this platform offer clip export at all?
    EXPORT_FFMPEG_BIN: str = "ffmpeg"             # a path or a name on PATH
    # Longest window a single export may cover. This knob existed and was NEVER READ: a caller
    # could ask for a year of footage and the worker would try. Bounded work is the difference
    # between a slow export and an outage.
    # How long a worker may hold an export job before another may take it. Sized to the LONGEST
    # transcode this deployment expects: reclaiming a job that is still running means two workers
    # transcoding the same clip, which is the cost the lease exists to bound. An export is the
    # longest-running work in this service, so this is deliberately generous.
    EXPORT_LEASE_SECONDS: int = 1_800
    EXPORT_MAX_RANGE_SECONDS: int = 3_600
    EXPORT_FORMATS: str = "mp4"                   # comma list; each needs an EXPORT_ARGS entry
    # ffmpeg arguments per output format, as JSON. `-c copy` is a REMUX: the same bitstream in a
    # new container — fast, lossless, and it cannot be accused of degrading evidence. Re-encoding
    # lives here too for platforms that need smaller files, and it is opt-in for that reason.
    EXPORT_ARGS: str = '{"mp4": ["-c", "copy", "-movflags", "+faststart"]}'
    EXPORT_CONTENT_TYPES: str = '{"mp4": "video/mp4"}'
    # What ffmpeg may follow from the concat list. Without a whitelist it will read anything a
    # list file points at, which turns a segment reference into an arbitrary-file-read.
    EXPORT_PROTOCOLS: str = "file,http,https,tcp,tls,crypto"
    EXPORT_TIMEOUT_SECONDS: int = 900             # 15 min; a hung export must not hold a worker
    EXPORT_SEGMENT_URL_TTL: int = 3_600           # long enough for ffmpeg to read every segment
    EXPORT_MAX_SEGMENTS: int = 5_000              # a bound on one clip's inputs
    EXPORT_MAX_RECORDINGS: int = 1_000            # how far back to look for overlapping segments
    EXPORT_DRAIN_MAX: int = 10                    # exports per worker run
    EXPORT_DOWNLOAD_TTL: int = 300                # signed download link lifetime

    @property
    def export_formats(self) -> tuple[str, ...]:
        return tuple(f.strip() for f in self.EXPORT_FORMATS.split(",") if f.strip())

    @property
    def export_args(self) -> dict[str, list[str]]:
        import json as _json
        try:
            parsed = _json.loads(self.EXPORT_ARGS or "{}")
        except ValueError as exc:
            raise ValueError(f"EXPORT_ARGS is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict) or not all(
                isinstance(k, str) and isinstance(v, list)
                and all(isinstance(a, str) for a in v) for k, v in parsed.items()):
            raise ValueError('EXPORT_ARGS must be {"format": ["-arg", ...]}')
        return parsed

    @property
    def export_content_types(self) -> dict[str, str]:
        import json as _json
        try:
            parsed = _json.loads(self.EXPORT_CONTENT_TYPES or "{}")
        except ValueError as exc:
            raise ValueError(f"EXPORT_CONTENT_TYPES is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError('EXPORT_CONTENT_TYPES must be {"format": "mime/type"}')
        return parsed

    # Quotas (per-tenant fairness; fail-closed via the entitlement pattern).
    STREAM_MAX_CONCURRENT_SESSIONS_GLOBAL: int = 100
    STREAM_MAX_CONCURRENT_SESSIONS_PER_TENANT: int = 10
    STREAM_MAX_HOURS_MONTH: int = 0              # 0 = unlimited
    STREAM_MAX_STORAGE_BYTES: int = 0            # 0 = unlimited

    # Security.
    STREAM_ALLOWED_HOSTS: str = ""              # comma list of host/CIDR; empty = any public host
    STREAM_ALLOW_PRIVATE: bool = False          # true for on-prem NVRs (still blocks metadata IPs)
    SEAL_KEY: str = ""                          # AES-GCM key for source credentials at rest
    PLAYBACK_URL_TTL_SECONDS: int = 60
    PLAYBACK_SIGNING_SECRET: str = ""           # falls back to INTERNAL token if empty

    # Health.
    RECONNECT_BACKOFF_MS: int = 500
    RECONNECT_MAX: int = 20
    SOURCE_OFFLINE_AFTER_SECONDS: int = 30
    SOURCE_PROBE_TTL_SECONDS: int = 60
    MOTION_DEBOUNCE_SECONDS: int = 30
    PTZ_MOVE_TIMEOUT_SECONDS: int = 5           # a dropped 'stop' must not spin the motor forever

    @property
    def default_outputs(self) -> list[str]:
        return [o.strip() for o in self.SESSION_DEFAULT_OUTPUTS.split(",") if o.strip()]

    @property
    def stream_allowed_hosts(self) -> frozenset[str]:
        return frozenset(h.strip().lower() for h in self.STREAM_ALLOWED_HOSTS.split(",") if h.strip())

    @property
    def turn_urls(self) -> list[str]:
        return [u.strip() for u in self.TURN_URLS.split(",") if u.strip()]

    @property
    def transcode_profiles(self) -> dict:
        import json
        if not self.TRANSCODE_PROFILES.strip():
            return {}
        try:
            return json.loads(self.TRANSCODE_PROFILES)
        except json.JSONDecodeError:
            return {}

    def assert_export_config_ok(self) -> None:
        """Boot guard. A platform that OFFERS clip export must be able to produce one.

        EXPORT_ENABLED is the same distinction CONSENT_GATED draws: it separates "this platform
        does not do clip export" from "somebody forgot to configure the exporter". Those look
        identical from the driver alone, and that ambiguity is how a model, a store and a config
        knob for exports sat here with nothing able to produce a clip.
        """
        self.export_args               # parse now: malformed JSON fails at boot, not mid-export
        self.export_content_types
        missing = [f for f in self.export_formats if f not in self.export_args]
        if missing:
            raise ValueError(
                f"EXPORT_FORMATS lists {missing} with no EXPORT_ARGS entry: an export in that "
                f"format would be accepted and then fail on the worker.")
        if not self.is_production or not self.EXPORT_ENABLED:
            return
        if self.EXPORT_DRIVER == "null":
            raise ValueError(
                "EXPORT_DRIVER=null in production with EXPORT_ENABLED=true: every export would be "
                "accepted and then fail. Set 'ffmpeg' with EXPORT_FFMPEG_BIN, or turn "
                "EXPORT_ENABLED off and stop advertising the feature.")

    def assert_stream_config_ok(self) -> None:
        """Boot guards: refuse footgun postures in production, fail-fast (never at first stream)."""
        if self.is_production:
            if self.STREAM_ENGINE_DRIVER == "null":
                raise ValueError("STREAM_ENGINE_DRIVER=null cannot serve real streams in production")
            if not self.SEAL_KEY:
                raise ValueError("SEAL_KEY is required in production to seal source credentials")
            if self.RECORDING_SINK_DRIVER == "file-service" and not self.RECORDING_FILE_SERVICE_URL:
                raise ValueError("RECORDING_FILE_SERVICE_URL is required when recording to file-service")
            if self.WEBRTC_ENABLED and self.MEDIA_EGRESS_DRIVER == "turn" and not self.TURN_URLS:
                raise ValueError("MEDIA_EGRESS_DRIVER=turn requires TURN_URLS")

        if self.SECRETS_KEK and (
                self.RECORDING_CREDENTIAL_REFRESH_SECONDS > self.SECRETS_ROTATION_WINDOW_SECONDS):
            # The refresh window is how long a replica may keep using the old credential; the
            # rotation window is how long the old one stays valid. If refresh outlives rotation the
            # two stop overlapping, and a rotation opens a gap where a live recorder uploads with a
            # token file-service has already stopped accepting — segments failing, silently, for a
            # change that looked successful in the panel. Footage lost that way cannot be re-taken.
            raise ValueError(
                f"RECORDING_CREDENTIAL_REFRESH_SECONDS ({self.RECORDING_CREDENTIAL_REFRESH_SECONDS}) "
                f"must not exceed SECRETS_ROTATION_WINDOW_SECONDS "
                f"({self.SECRETS_ROTATION_WINDOW_SECONDS}) — otherwise a rotation leaves a window "
                f"where the sink uploads with a credential the store no longer accepts")

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
