# =============================================================================
# identity-service — Public JWKS (the verify half of the signing keyring)
# -----------------------------------------------------------------------------
# `/.well-known/jwks.json` publishes the PUBLIC keys the rest of the fleet uses to verify tokens
# identity signs. It is deliberately UNAUTHENTICATED: a public key is public by definition, and a
# verifier that had to authenticate to fetch it would need a credential to check a credential. Only
# public halves ever appear here — the private keys never leave the signing process.
#
# During a rotation overlap this returns BOTH the new and the retiring public key, each tagged with
# its `kid`, so a token signed by either verifies until the retiring key's window lapses. When
# rotation is disabled the set is empty (nothing is published), which a jwks verifier treats as "no
# key yet" rather than a forgery.
# =============================================================================
from __future__ import annotations

from fastapi import APIRouter, Request, Response
import json

router = APIRouter(tags=["jwks"])


@router.get("/.well-known/jwks.json")
async def jwks(request: Request) -> Response:
    sks = getattr(request.app.state, "signing_key_service", None)
    body = sks.jwks() if sks is not None else {"keys": []}
    # A short, explicit cache lifetime: long enough to spare identity a request per verify, short
    # enough that a rotated key propagates within it. Fleet verifiers cache on their own timer too.
    return Response(content=json.dumps(body), media_type="application/json",
                    headers={"Cache-Control": "public, max-age=60"})
