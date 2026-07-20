# =============================================================================
# onboarding-service — Configuration (fail-fast, ZERO-HARDCODED)
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
    SERVICE_NAME: str = "onboarding-service"
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
    # Salts the subject pseudonym on an erasure ack. FROZEN — config/product.identifiers.lock.json.
    PII_SALT: str = ""
    ERASURE_MAX_SCAN: int = 100_000   # a DSAR is rare, but must never be an unbounded read

    # ── onboarding (Phase 41) — flows declare WHAT, config decides HOW ────────
    # Where each owning service lives, so a requirement can be evaluated by asking it. Same
    # name=value idiom as PRIVACY_TARGETS. A source not listed answers UNKNOWN — never satisfied.
    #   "identity=http://identity-service:8000,verification=http://verification-service:8000"
    ONBOARDING_SOURCES: str = ""
    # Which fact-path to call on each source, so a customer's own service can answer requirements
    # without matching our URL shape.
    #   "identity=/internal/identity/facts/{fact}?tenant={tenant}&subject={subject}"
    ONBOARDING_SOURCE_PATHS: str = ""
    ONBOARDING_SOURCE_ANSWER_PATH: str = "satisfied"   # where the yes/no lives in the reply
    ONBOARDING_SOURCE_TIMEOUT_SECONDS: int = 5
    # How a grant is actually delivered. A platform with no wallet picks trial_period or plan.
    ONBOARDING_ENTITLEMENT: str = "null"      # null | wallet | trial | plan
    ONBOARDING_WALLET_URL: str = ""
    # What each declared grant is worth. Money in MINOR units — no float ever touches an amount.
    #   "welcome=amount:200000,currency:SAR" or "welcome=days:14"
    ONBOARDING_GRANTS: str = ""
    # Provisioning: what signup CREATES, and where. Each kind names a create and an undo endpoint,
    # because a create without a matching undo is how orphans happen.
    #   "tenant=http://tenant-service:8000/internal/tenants|/internal/tenants/{id}"
    ONBOARDING_PROVISIONERS: str = ""
    ONBOARDING_PROVISION_TIMEOUT_SECONDS: int = 10

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
    # How long a client should wait before retrying an action we REFUSED because the audit log
    # was unreachable. It is a 503 + Retry-After, never a 500: the action did not happen, nothing
    # committed, and trying again later is the correct response. A 500 would tell the caller we
    # crashed, which invites a bug report instead of a retry.
    AUDIT_RETRY_AFTER_SECONDS: int = 30
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
    def onboarding_sources(self) -> dict[str, str]:
        """`service -> base_url`. Malformed fails at BOOT: a source we cannot reach makes every
        requirement it owns UNKNOWN, and a flow that can never complete is a customer stuck."""
        out: dict[str, str] = {}
        for part in self.ONBOARDING_SOURCES.split(","):
            part = part.strip()
            if not part:
                continue
            name, sep, url = part.partition("=")
            name, url = name.strip(), url.strip()
            if not sep or not name or not url:
                raise ValueError(f"ONBOARDING_SOURCES entry {part!r} must be service=base_url")
            if not url.startswith(("http://", "https://")):
                raise ValueError(f"ONBOARDING_SOURCES url {url!r} must start with http(s)://")
            out[name] = url.rstrip("/")
        return out

    @property
    def onboarding_source_paths(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for part in self.ONBOARDING_SOURCE_PATHS.split(","):
            part = part.strip()
            if not part:
                continue
            name, sep, path = part.partition("=")
            name, path = name.strip(), path.strip()
            if not sep or not name or not path.startswith("/"):
                raise ValueError(
                    f"ONBOARDING_SOURCE_PATHS entry {part!r} must be service=/relative/path")
            out[name] = path
        return out

    @property
    def onboarding_grants(self) -> dict[str, dict[str, str]]:
        """`kind -> {amount, currency, days}`. A grant a flow declares but config never defines is
        refused at issue time rather than silently granting nothing."""
        out: dict[str, dict[str, str]] = {}
        for part in self.ONBOARDING_GRANTS.split(","):
            part = part.strip()
            if not part:
                continue
            kind, sep, spec = part.partition("=")
            kind = kind.strip()
            if not sep or not kind:
                raise ValueError(f"ONBOARDING_GRANTS entry {part!r} must be kind=key:value|…")
            fields: dict[str, str] = {}
            for pair in spec.split("|"):
                key, colon, value = pair.strip().partition(":")
                if not colon or not key.strip():
                    raise ValueError(f"ONBOARDING_GRANTS spec {pair!r} must be key:value")
                fields[key.strip()] = value.strip()
            if "amount" in fields and not fields["amount"].isdigit():
                # Money is integer minor units. A float here is a rounding bug in a gift.
                raise ValueError(
                    f"ONBOARDING_GRANTS {kind!r} amount must be integer minor units, "
                    f"got {fields['amount']!r}")
            out[kind] = fields
        return out

    @property
    def onboarding_provisioners(self) -> dict[str, tuple[str, str]]:
        """`kind -> (create_url, undo_url)`. Both required: an adapter that can create and cannot
        undo is precisely the shape that leaves orphans."""
        out: dict[str, tuple[str, str]] = {}
        for part in self.ONBOARDING_PROVISIONERS.split(","):
            part = part.strip()
            if not part:
                continue
            kind, sep, urls = part.partition("=")
            create, pipe, undo = urls.partition("|")
            kind, create, undo = kind.strip(), create.strip(), undo.strip()
            if not sep or not kind or not create or not pipe or not undo:
                raise ValueError(
                    f"ONBOARDING_PROVISIONERS entry {part!r} must be kind=create_url|undo_url")
            if not create.startswith(("http://", "https://")):
                raise ValueError(f"ONBOARDING_PROVISIONERS create url {create!r} must be http(s)")
            if "{id}" not in undo:
                raise ValueError(
                    f"ONBOARDING_PROVISIONERS undo url {undo!r} must contain {{id}} — without it "
                    f"compensation cannot name what to remove")
            out[kind] = (create, undo)
        return out

    def assert_onboarding_config_ok(self) -> None:
        """Boot guard.

        The catastrophe: a flow promises a welcome credit, the entitlement backend is `null`, and
        every customer completes onboarding believing they were given something. The platform looks
        healthy and every dashboard shows the feature on.
        """
        self.onboarding_sources
        self.onboarding_source_paths
        self.onboarding_provisioners
        grants = self.onboarding_grants

        if not self.is_production:
            return
        from app.core.flows import validate
        from app.onboarding_flows import ONBOARDING_FLOWS

        flows = validate(ONBOARDING_FLOWS)
        granting = {r.get("grants") for f in flows for r in f["requirements"] if r.get("grants")}
        if self.ONBOARDING_ENTITLEMENT == "wallet" and not self.ONBOARDING_WALLET_URL:
            raise ValueError(
                "ONBOARDING_ENTITLEMENT=wallet but ONBOARDING_WALLET_URL is empty: every grant "
                "would fail while the flow reported a credit the customer never received")
        if granting and self.ONBOARDING_ENTITLEMENT == "null":
            raise ValueError(
                f"flows declare grants ({', '.join(sorted(granting))}) but "
                f"ONBOARDING_ENTITLEMENT is null: onboarding would complete and the customer "
                f"would be told they received something nobody issued.")
        for kind in granting:
            if kind not in grants:
                raise ValueError(
                    f"a flow grants {kind!r} but ONBOARDING_GRANTS does not define it — the grant "
                    f"would be refused at issue time and the flow could never complete")

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
