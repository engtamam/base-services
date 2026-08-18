"""Object-store credential resolution — one provider seam, selected by config, not one cloud.

The S3 adapter authenticates in one of four ways, chosen by OBJECTSTORE_CREDENTIALS:

  static           the two explicit keys. TODAY'S behaviour and still the DEFAULT, so nothing moves
                   under an existing deployment. Half a credential still refuses (see build_session).
  imds             botocore's OWN ambient chain, untouched — AWS EC2 instance profile / ECS task role.
  alibaba-ecs-ram  the Alibaba Cloud ECS instance RAM role metadata service. Its dialect is NOT AWS
                   IMDS: the endpoint lives elsewhere and the JSON names AccessKeySecret / SecurityToken
                   (not SecretAccessKey / Token), so botocore's own imds provider cannot read it — which
                   is why OBJECTSTORE_ACCESS_KEY="" on Alibaba does not silently work, it silently FAILS.
                   Verified against Alibaba's documentation:
                     GET {endpoint}/latest/meta-data/ram/security-credentials/{role}
                     -> {"AccessKeyId","AccessKeySecret","SecurityToken","Expiration","Code":"Success"}
  sts-endpoint     a GENERIC HTTP endpoint returning key / secret / token / expiry, with the field
                   NAMES configurable — so the cloud after this one is a config change, not a release.

Short-lived instance-role tokens refresh THROUGH botocore's own refreshable-credential machinery
(AioRefreshableCredentials): refresh-before-expiry is botocore's job, not a hand-rolled timer, and a
refresh failure surfaces as a named CredentialRetrievalError — never a 403 on the next upload. For the
metadata modes the provider chain is REPLACED with only this one provider, so a failure is a named
error rather than a silent fall-through to env/imds/static.

The metadata endpoint ADDRESS is never baked into this code — it is deploy-time config
(OBJECTSTORE_METADATA_ENDPOINT). Where the metadata service lives is a property of the cloud (Alibaba
serves it at one address, a kube2ram sidecar or a mock at another), the same way the CronJob timeZone
is a property of where the product is sold. Only the DIALECT of a named mode (its path and field
names) lives here — that is the definition of the mode, like botocore hard-defines AWS IMDS.

⚠ THE ROLE IS NODE-WIDE. An instance RAM role is granted to the MACHINE, not the pod. On a single-node
k3s cluster every pod that can reach the metadata address inherits it, so this credential is readable
by every service on the box. The isolation lives in the NETWORK layer — a NetworkPolicy that permits
egress to the metadata address only from the service that must hold the credential (backup-service) —
NOT in this adapter. A reader wiring a new service to a RAM role must add that policy too.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.ports.errors import ConfigError

STATIC = "static"
IMDS = "imds"
ALIBABA_ECS_RAM = "alibaba-ecs-ram"
STS_ENDPOINT = "sts-endpoint"
MODES = (STATIC, IMDS, ALIBABA_ECS_RAM, STS_ENDPOINT)

# The Alibaba ECS RAM DIALECT — the definition of the alibaba-ecs-ram mode, not a tunable. The endpoint
# address is config; this is the protocol spoken at it. Field names verified against Alibaba's docs.
_ALIBABA_ROLE_PATH = "/latest/meta-data/ram/security-credentials/"
_ALIBABA_TOKEN_PATH = "/latest/api/token"          # IMDSv2-style hardening (best-effort)
_ALIBABA_FIELDS = {"access_key": "AccessKeyId", "secret_key": "AccessKeySecret",
                   "token": "SecurityToken", "expiry_time": "Expiration"}

_PROVIDER_METHOD = "objectstore-metadata"


@dataclass(frozen=True)
class CredentialConfig:
    """Everything the resolver needs, read from Settings by the container — never from a request."""
    mode: str = STATIC
    access_key: str = ""
    secret_key: str = ""
    endpoint: str = ""                     # metadata / STS base URL (required for the metadata modes)
    role: str = ""                         # alibaba: optional — auto-discovered from the service if ""
    metadata_token_ttl: int = 21600        # alibaba IMDSv2-style token TTL seconds (a protocol knob)
    fetch_timeout: float = 5.0             # bound the metadata call; a hung endpoint must not hang boot
    # sts-endpoint: which JSON keys carry each value. Defaults to the common Alibaba STS names, all
    # overridable so a different provider is a values change. An empty token/expiry field means the
    # provider issues non-expiring creds (rare; the endpoint owns rotation then).
    sts_fields: dict[str, str] = field(default_factory=lambda: dict(_ALIBABA_FIELDS))


def build_session(aioboto3_mod: Any, cfg: CredentialConfig) -> tuple[Any, dict[str, str]]:
    """Return (session, client_kwargs) for the S3 adapter. Validates SYNCHRONOUSLY so a misconfigured
    provider fails at STARTUP where a human is standing, not at the first nightly backup.

    - static  -> a plain session; the keys go to the client (today's path).
    - imds    -> a plain session; botocore's ambient chain resolves at call time.
    - metadata modes -> a session whose credential chain is REPLACED with one refreshable provider.
    """
    mode = cfg.mode or STATIC
    if mode not in MODES:
        # Refused, never silently treated as static (criterion 4).
        raise ConfigError(
            f"OBJECTSTORE_CREDENTIALS={mode!r} is not one of {', '.join(MODES)} — an unknown "
            f"credential mode must fail at startup, not resolve to the wrong provider at first use")

    if mode == STATIC:
        if cfg.access_key and cfg.secret_key:
            return aioboto3_mod.Session(), {"aws_access_key_id": cfg.access_key,
                                            "aws_secret_access_key": cfg.secret_key}
        if cfg.access_key or cfg.secret_key:
            # The existing "half a credential" refusal — kept exactly (criterion 5).
            raise ConfigError(
                "OBJECTSTORE_ACCESS_KEY and OBJECTSTORE_SECRET_KEY must be set together — half a "
                "credential authenticates nothing, and silently falling back to the ambient chain "
                "would hide the misconfiguration until the first upload")
        return aioboto3_mod.Session(), {}          # empty => ambient chain, as before

    if mode == IMDS:
        return aioboto3_mod.Session(), {}          # botocore's own chain, untouched

    # ── metadata modes: a refreshable provider, endpoint required at startup ──────────────────
    if not cfg.endpoint:
        raise ConfigError(
            f"OBJECTSTORE_CREDENTIALS={mode} requires OBJECTSTORE_METADATA_ENDPOINT — the metadata "
            f"service address is a property of the cloud and is never assumed by the code")
    if mode == STS_ENDPOINT:
        for req in ("access_key", "secret_key"):
            if not cfg.sts_fields.get(req):
                raise ConfigError(
                    f"OBJECTSTORE_CREDENTIALS=sts-endpoint requires the JSON field name for {req!r}")

    fetch = _make_fetch(mode, cfg)
    session = _session_with_provider(aioboto3_mod, fetch)
    return session, {}


# ── session assembly (lazy botocore/aiobotocore imports — only for the metadata modes) ─────────
def _session_with_provider(aioboto3_mod: Any, fetch) -> Any:
    from aiobotocore.session import AioSession
    from aiobotocore.credentials import AioRefreshableCredentials
    from botocore.credentials import CredentialProvider

    class _RefreshableMetadataProvider(CredentialProvider):
        METHOD = _PROVIDER_METHOD
        CANONICAL_NAME = "ObjectStoreMetadata"

        async def load(self):
            # The FIRST fetch also proves reachability; a failure here raises a named error at first
            # use. refresh_using is the SAME fetch, so botocore refreshes before expiry on its own.
            metadata = await fetch()
            return AioRefreshableCredentials.create_from_metadata(
                metadata=metadata, refresh_using=fetch, method=self.METHOD)

    boto = AioSession()
    resolver = boto.get_component("credential_provider")
    # ONLY our provider — no env/imds/static behind it, so a metadata failure is a named error, never a
    # silent fall-through to a different credential (criterion 4).
    resolver.providers = [_RefreshableMetadataProvider()]
    return aioboto3_mod.Session(botocore_session=boto)


def _make_fetch(mode: str, cfg: CredentialConfig):
    if mode == ALIBABA_ECS_RAM:
        async def fetch():
            return await _fetch_alibaba(cfg)
        return fetch
    async def fetch():                                  # noqa: E306 — sts-endpoint
        return await _fetch_sts(cfg)
    return fetch


# ── the fetchers (async; run inside botocore's credential resolution) ──────────────────────────
def _retrieval_error(msg: str):
    from botocore.exceptions import CredentialRetrievalError
    return CredentialRetrievalError(provider=_PROVIDER_METHOD, error_msg=msg)


async def _get_json(url: str, headers: dict[str, str], timeout: float) -> Any:
    import aiohttp
    to = aiohttp.ClientTimeout(total=max(0.1, float(timeout)))
    async with aiohttp.ClientSession(timeout=to) as sess:
        async with sess.get(url, headers=headers) as resp:
            if resp.status != 200:
                raise _retrieval_error(f"GET {url} -> HTTP {resp.status}")
            return await resp.json(content_type=None)          # metadata services omit the JSON mimetype


async def _fetch_alibaba(cfg: CredentialConfig) -> dict[str, Any]:
    import aiohttp
    base = cfg.endpoint.rstrip("/")
    to = aiohttp.ClientTimeout(total=max(0.1, float(cfg.fetch_timeout)))
    async with aiohttp.ClientSession(timeout=to) as sess:
        # IMDSv2-style hardening: a metadata-access token, best-effort — a service in hardened mode
        # requires it, one in normal mode ignores it, so sending it when present is always safe.
        headers: dict[str, str] = {}
        try:
            async with sess.put(base + _ALIBABA_TOKEN_PATH,
                                headers={"X-aliyun-ecs-metadata-token-ttl-seconds":
                                         str(int(cfg.metadata_token_ttl))}) as tr:
                if tr.status == 200:
                    headers["X-aliyun-ecs-metadata-token"] = (await tr.text()).strip()
        except aiohttp.ClientError:
            pass                                          # normal (non-hardened) mode needs no token

        role = cfg.role
        if not role:
            # The role is granted to the instance; the listing endpoint names it. Auto-discovery means
            # an operator need not restate what the cloud already knows.
            async with sess.get(base + _ALIBABA_ROLE_PATH, headers=headers) as rr:
                if rr.status != 200:
                    raise _retrieval_error(
                        f"could not list the instance RAM role at {base + _ALIBABA_ROLE_PATH} "
                        f"(HTTP {rr.status}) — attach a RAM role, or set OBJECTSTORE_METADATA_ROLE")
                listing = (await rr.text()).strip()
            role = listing.splitlines()[0].strip() if listing else ""
            if not role:
                raise _retrieval_error("the instance RAM role listing was empty — no role is attached")

        async with sess.get(base + _ALIBABA_ROLE_PATH + role, headers=headers) as cr:
            if cr.status != 200:
                raise _retrieval_error(f"RAM role credentials -> HTTP {cr.status} for role {role!r}")
            data = await cr.json(content_type=None)

    if str(data.get("Code", "Success")) != "Success":
        raise _retrieval_error(f"metadata Code={data.get('Code')!r} for role {role!r}")
    return _map_fields(data, _ALIBABA_FIELDS, where=f"Alibaba RAM role {role!r}")


async def _fetch_sts(cfg: CredentialConfig) -> dict[str, Any]:
    data = await _get_json(cfg.endpoint, {}, cfg.fetch_timeout)
    if not isinstance(data, dict):
        raise _retrieval_error(f"sts-endpoint {cfg.endpoint} did not return a JSON object")
    return _map_fields(data, cfg.sts_fields, where=f"sts-endpoint {cfg.endpoint}")


def _map_fields(data: dict, fields: dict[str, str], *, where: str) -> dict[str, Any]:
    """Map a provider's JSON onto botocore's {access_key, secret_key, token, expiry_time}. access_key
    and secret_key are mandatory; token/expiry are optional (a provider issuing static creds omits
    them). A missing mandatory field is a named error, not a half credential handed to boto."""
    out: dict[str, Any] = {}
    for canon in ("access_key", "secret_key"):
        name = fields.get(canon)
        value = data.get(name) if name else None
        if not value:
            raise _retrieval_error(f"{where}: response has no {canon} (expected JSON field {name!r})")
        out[canon] = value
    out["token"] = data.get(fields["token"]) if fields.get("token") else None
    out["expiry_time"] = data.get(fields["expiry_time"]) if fields.get("expiry_time") else None
    return out
