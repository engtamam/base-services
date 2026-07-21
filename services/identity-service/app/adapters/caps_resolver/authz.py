"""AuthzCaps — capabilities derived from roles by authz-service, at mint time.

    GET {AUTHZ_URL}/internal/authz/effective?subject=&tenant=&roles=a,b   -> {"capabilities": [...]}

Identity passes the roles it owns; authz expands them into what they MEAN. Edit the
`compliance-manager` role in authz and everyone holding it gets the new capabilities on their next
mint — nothing typed by hand, nothing to re-issue by script.

THREE THINGS THIS HAS TO GET RIGHT, because it sits on the mint path:

1. IT IS THE HOT PATH. Login, tenant switch and every refresh mint a token. An un-cached HTTP call
   there turns authz into a hard dependency of every session on the platform. Answers are cached
   per (subject, tenant, roles) for CAPS_TTL seconds — short, because it is also the revocation
   window: shrink a role's capabilities and the change is live within the TTL.

2. IT FAILS CLOSED. If authz cannot be reached we raise, and no token is minted. We do NOT mint a
   token with no capabilities: a zero-cap token looks exactly like a permissions bug, and support
   "fixes" permissions bugs by granting capabilities by hand — so a five-minute authz outage
   becomes a permanent over-grant nobody ever revokes.

3. THE URL IS NOT A PLACE TO BE RELAXED. Identity holds the internal token; a caller who could
   point AUTHZ_URL at a host they control would be handed it, and could then answer the question
   "what may this person do?" with "anything". The URL is config, never derived from a request.
"""
from __future__ import annotations

import time

from app.adapters.lazy import load
from app.ports.caps_resolver import CapsUnavailable
from app.ports.errors import ConfigError


class AuthzCaps:
    driver = "authz"

    def __init__(self, base_url: str, internal_token: str, *,
                 timeout_ms: int, ttl_seconds: int, user_agent: str = "",
                 path: str, token_header: str, caps_field: str,
                 subject_param: str, tenant_param: str, roles_param: str,
                 roles_sep: str) -> None:
        # Everything about the WIRE is config. The path, the header the token rides in, the field
        # the capabilities come back in, the parameter names, the separator. A platform that puts
        # its authority behind a different contract — or in front of a gateway that renames things
        # — swaps env vars, not code. The one thing that is NOT negotiable is the semantics: roles
        # go in, capabilities come out, and an unreachable authority mints nothing.
        if not base_url:
            raise ConfigError(
                "IDENTITY_CAPS_SOURCE=authz requires AUTHZ_URL — a capability authority that is "
                "not configured cannot be consulted, and identity will not guess what a role means"
            )
        if not internal_token:
            # /internal/authz/effective is behind the internal token. Without one every mint would
            # 401 and nobody could log in — better to say so at BOOT than at 3am.
            raise ConfigError(
                "IDENTITY_CAPS_SOURCE=authz requires AUTHZ_INTERNAL_TOKEN to call "
                "/internal/authz/effective"
            )
        self._httpx = load("httpx", "authz")
        self._url = base_url.rstrip("/") + "/" + path.lstrip("/")
        self._token = internal_token
        self._timeout = timeout_ms / 1000
        self._ttl = ttl_seconds
        self._ua = user_agent
        self._token_header = token_header
        self._caps_field = caps_field
        self._subject_param = subject_param
        self._tenant_param = tenant_param
        self._roles_param = roles_param
        self._sep = roles_sep
        self._cache: dict[tuple[str, str, str], tuple[float, list[str]]] = {}
        # Set by the composition root when an operator credential store exists. Resolving at USE
        # time is the whole point: a token captured once at construction is never read again for
        # the life of the process, which is precisely why changing it used to need a redeploy. The
        # configured value stays the fallback, so an unmigrated deployment is unchanged.
        self._token_provider = None

    def set_token_provider(self, provider) -> None:
        """An async callable returning the current internal token."""
        self._token_provider = provider

    async def _current_token(self) -> str:
        if self._token_provider is None:
            return self._token
        return await self._token_provider()

    async def resolve(self, *, subject: str, tenant: str, roles: list[str],
                      caps: list[str]) -> list[str]:
        key = (subject, tenant, self._sep.join(sorted(roles)))
        hit = self._cache.get(key)
        now = time.monotonic()
        if hit and hit[0] > now:
            # The direct grant is unioned OUTSIDE the cache: it comes from the membership row we
            # already hold, so it costs nothing and must never be stale relative to that row.
            return sorted(set(hit[1]) | set(caps))

        headers = {self._token_header: await self._current_token()}
        if self._ua:
            headers["User-Agent"] = self._ua
        try:
            async with self._httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.get(self._url, headers=headers, params={
                    self._subject_param: subject,
                    self._tenant_param: tenant,
                    self._roles_param: self._sep.join(roles)})
            if r.status_code != 200:
                raise CapsUnavailable(
                    f"authz-service answered {r.status_code} resolving capabilities")
            derived = r.json().get(self._caps_field)
            if not isinstance(derived, list):
                # A malformed body is not "no capabilities". Reading a broken answer as an empty
                # set is how a parse bug becomes a silent, total de-authorization.
                raise CapsUnavailable("authz-service returned a malformed capabilities body")
        except CapsUnavailable:
            raise
        except Exception as exc:                     # network, DNS, timeout, TLS, bad JSON
            raise CapsUnavailable(f"authz-service unreachable: {exc.__class__.__name__}") from exc

        derived = [str(c) for c in derived]
        self._cache[key] = (now + self._ttl, derived)
        return sorted(set(derived) | set(caps))
