"""SharedTokenCaller — the flat fleet-wide token, the non-breaking default.

Authenticates that the caller holds INTERNAL_SERVICE_TOKEN, but not WHICH service it is: the returned
identity is an unnamed-but-authenticated peer (caller=""). This is exactly today's behaviour, kept as
a first-class driver so an unconfigured deployment is unchanged and a deployment can move to `static`
/`jwks` when it wants verifiable per-caller identity. Constant-time compare (CWE-208).
"""
from __future__ import annotations

import hmac

from app.ports.internal_caller import CallerIdentity, InternalCallerError


class SharedTokenCaller:
    driver = "shared"

    def __init__(self, expected_token: str) -> None:
        self._expected = expected_token or ""

    def verify(self, *, token: str, assertion: str, audience: str) -> CallerIdentity:
        # An empty configured token must NEVER accept an empty presented token — that would open the
        # whole internal tier. compare_digest handles the comparison; the guard rejects the empty case.
        if self._expected and hmac.compare_digest(token or "", self._expected):
            return CallerIdentity(caller="", via="shared")
        raise InternalCallerError("internal token mismatch")
