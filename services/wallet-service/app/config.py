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

    # ── Internal service-to-service auth (REQUIRED) ─────────────────────────
    # Shared token for /internal/* endpoints. Must be strong (>= 32 chars).
    INTERNAL_SERVICE_TOKEN: str = Field(min_length=32)

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
    OBJECTSTORE_ROOT: str = "/tmp/objectstore"  # local driver; under the chart's
    #                                            writable /tmp mount, not the read-only
    #                                            root. production uses s3 + a bucket.
    OBJECTSTORE_BUCKET: str = ""  # s3 driver
    OBJECTSTORE_ENDPOINT_URL: str = ""  # s3-compatible endpoint (config allowlist)

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
    WALLET_IDEMPOTENCY_TTL_SECONDS: int = 86_400
    LEDGER_LIST_LIMIT: int = 100              # a statement endpoint must never dump the ledger
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
    def wallet_funds(self) -> tuple[tuple[str, int, bool], ...]:
        """[(name, priority, taxable)] ordered by spend priority. Fail-fast on nonsense."""
        from app.ports.errors import ConfigError  # noqa: PLC0415 — avoid an import cycle

        out: list[tuple[str, int, bool]] = []
        for chunk in self.WALLET_FUNDS.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            parts = chunk.split(":")
            if len(parts) != 3:
                raise ConfigError(f"WALLET_FUNDS entry must be name:priority:taxability — got {chunk!r}")
            name, priority, taxability = (p.strip() for p in parts)
            if not name:
                raise ConfigError("WALLET_FUNDS: empty fund name")
            if taxability not in ("taxed", "untaxed"):
                raise ConfigError(f"WALLET_FUNDS: taxability must be taxed|untaxed — got {taxability!r}")
            try:
                pri = int(priority)
            except ValueError as exc:
                raise ConfigError(f"WALLET_FUNDS: priority must be an integer — got {priority!r}") from exc
            out.append((name, pri, taxability == "taxed"))
        if not out:
            raise ConfigError("WALLET_FUNDS must define at least one fund")
        names = [f[0] for f in out]
        if len(set(names)) != len(names):
            raise ConfigError(f"WALLET_FUNDS: duplicate fund name in {names}")
        return tuple(sorted(out, key=lambda f: f[1]))

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
