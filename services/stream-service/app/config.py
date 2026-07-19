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

    # ── Internal service-to-service auth (REQUIRED) ─────────────────────────
    # Shared token for /internal/* endpoints. Must be strong (>= 32 chars).
    INTERNAL_SERVICE_TOKEN: str = Field(min_length=32)
    # Salts the subject pseudonym on an erasure ack. FROZEN — config/product.identifiers.lock.json.
    PII_SALT: str = ""
    ERASURE_MAX_SCAN: int = 100_000   # a DSAR is rare, but must never be an unbounded read

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
    def is_production(self) -> bool:
        return self.ENV == "production"

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

    # Retention & export.
    RETENTION_DAYS: int = 30
    RETENTION_MAX_BYTES: int = 0                  # 0 = unlimited (age-based only)
    EXPORT_MAX_RANGE_SECONDS: int = 3_600

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
