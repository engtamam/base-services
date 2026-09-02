"""InternalCaller port — who is calling this service's /internal tier, and may they.

The flat fleet-wide INTERNAL_SERVICE_TOKEN authenticates that a caller is SOME trusted service, never
WHICH one — so any holder reaches every service's whole /internal surface. This port adds a verifiable
caller IDENTITY as a pluggable driver, so a deployment chooses its posture from config (the platform
supports all; each picks):

  * shared     — the flat token only (today's behaviour, the non-breaking default).
  * static     — a short-TTL RS256 assertion (iss=caller, aud=this service), verified against a
                 statically-provisioned public key per caller. Unforgeable per-caller, no runtime fetch.
  * jwks       — the same assertion, verified against each caller's JWKS (reuses the fleet's rotation).

A driver returns a CallerIdentity (with the resolved `caller` name) or raises to deny. The per-route
policy layer above then decides whether THAT caller may reach the requested route; an unset policy
allows any authenticated caller, so enabling identity never breaks an existing call path on its own.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class CallerIdentity:
    """The verified caller. `caller` is the service name (assertion `iss`), or "" when the driver
    proved trust without a distinct identity (the shared-token driver) — an unnamed but authenticated
    peer. `via` names the driver that vouched, for the audit trail."""
    caller: str = ""
    via: str = "shared"


class InternalCallerError(Exception):
    """The caller could not be authenticated. Mapped to 401 by the internal tier — never leaks which
    check failed (missing/expired/wrong-audience/bad-signature all look identical to the caller)."""


@runtime_checkable
class InternalCaller(Protocol):
    driver: str

    def verify(self, *, token: str, assertion: str, audience: str) -> CallerIdentity:
        """Authenticate the caller from the presented credentials, or raise InternalCallerError.

        `token` is the X-Internal-Token header, `assertion` the X-Internal-Assertion header, `audience`
        this service's own name (the assertion's required `aud`). Pure and side-effect free so it is
        trivially testable and cannot itself become an oracle."""
        ...
