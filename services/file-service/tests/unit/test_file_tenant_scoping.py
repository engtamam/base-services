"""A missing tenant is not a wildcard.

THE BUG, reproduced before it was fixed. The scoping guard read:

    if f.tenant and principal.tenant and f.tenant != principal.tenant and "platform.files" not in caps

which short-circuits to ALLOWED whenever either side is empty — and both can be. `_extract_principal`
sets `tenant=None` when the token carries no tenant claim, and `upload()` stores `tenant=""` for
exactly such a caller. So:

    a token with NO tenant claim could read every tenant's files
    a file stored WITHOUT a tenant was readable by anybody

Nothing failed. Every record was internally consistent: the file existed, the caller was
authenticated, the check ran and passed.
"""
from __future__ import annotations

import asyncio
import types

import pytest
from fastapi import HTTPException

from app.api.auth import Principal
from app.api.v1.files import _load_scoped     # THE REAL GUARD, not a copy of it


class _Svc:
    """Only what _load_scoped touches: get(), raising NotFound for a missing id."""

    def __init__(self, tenant):
        self._tenant = tenant

    async def get(self, file_id):
        from app.ports.errors import NotFound
        if file_id != "file_1":
            raise NotFound(file_id)
        return types.SimpleNamespace(id=file_id, tenant=self._tenant)


def _guard(file_tenant, caller_tenant, caps=frozenset()):
    """Call the SHIPPED guard. An earlier version of this file re-implemented the predicate
    inline — every test passed against a copy of the code, and reverting the fix in files.py
    changed nothing. A test that does not execute the thing it names is not a test."""
    return asyncio.run(
        _load_scoped(_Svc(file_tenant), "file_1",
                     Principal(subject="u", caps=caps, tenant=caller_tenant)))


def test_a_caller_from_another_tenant_is_refused():
    with pytest.raises(HTTPException):
        _guard("acme", "globex")


def test_a_caller_with_NO_TENANT_CLAIM_is_refused():
    """THE hole. This used to be allowed, for every file on the platform."""
    with pytest.raises(HTTPException):
        _guard("acme", None)


def test_a_file_with_NO_TENANT_is_not_world_readable():
    """A file stored without a tenant belongs to nobody, so it is not everybody's."""
    with pytest.raises(HTTPException):
        _guard("", "globex")


def test_a_file_with_no_tenant_is_not_reachable_by_a_caller_with_no_tenant_either():
    """Two absences do not make a match. This is the case a naive `==` would let through."""
    with pytest.raises(HTTPException):
        _guard("", None)


def test_the_owning_tenant_still_reads_its_own_file():
    assert _guard("acme", "acme").tenant == "acme"


def test_the_platform_reader_is_deliberately_unscoped():
    """platform.files is the cross-tenant capability, and it is explicit rather than a side
    effect of a missing value."""
    assert _guard("acme", "globex", frozenset({"platform.files"})).tenant == "acme"


def test_the_platform_reader_can_reach_an_untenanted_file():
    """Someone has to be able to clean these up."""
    assert _guard("", None, frozenset({"platform.files"})).tenant == ""


# ── the internal minting endpoint ─────────────────────────────────────────────
# NB: these were once inspect.getsource() assertions, which never CALLED the endpoint — so a runtime
# NameError in it (build_files was never imported at module scope, file-001) passed every check while
# the endpoint 500'd in production. They are now real request-level tests through the app (file-002).
import datetime as _dt
import os as _os

import jwt as _jwt
from fastapi.testclient import TestClient as _TestClient

_SECRET = "h" * 40
_ITOKEN = "t" * 40


def _internal_app() -> _TestClient:
    from app.config import get_settings
    _os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN=_ITOKEN,
                       TOKEN_VERIFIER_DRIVER="hs256", TOKEN_HS256_SECRET=_SECRET,
                       OBJECTSTORE_DRIVER="memory", SIGNED_URL_SECRET="s" * 32, RATELIMIT_MAX="1000")
    get_settings.cache_clear()
    from app.main import create_app
    return _TestClient(create_app(), raise_server_exceptions=False)


def _upload_as(client, tenant):
    now = _dt.datetime.now(_dt.timezone.utc)
    tok = _jwt.encode({"sub": "u", "caps": ["files.upload", "files.read"], "tenant": tenant,
                       "exp": now + _dt.timedelta(minutes=5), "iat": now}, _SECRET, algorithm="HS256")
    r = client.post("/api/v1/files", content=b"\x89PNG\r\n\x1a\nx",
                    headers={"Authorization": "Bearer " + tok,
                             "Content-Type": "image/png", "X-Filename": "p.png"})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def test_internal_signed_url_mints_a_link_for_the_owning_tenant():
    # file-001/002: a real POST must return 200 with a token and a config TTL — proving the endpoint
    # actually runs (no NameError) and does not accept a caller-supplied lifetime.
    c = _internal_app()
    fid = _upload_as(c, "acme")
    r = c.post(f"/internal/files/{fid}/signed-url",
               headers={"X-Internal-Token": _ITOKEN}, params={"tenant": "acme"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["url"] and body["expires_in"] > 0


def test_internal_signed_url_404s_on_a_tenant_mismatch():
    c = _internal_app()
    fid = _upload_as(c, "acme")
    r = c.post(f"/internal/files/{fid}/signed-url",
               headers={"X-Internal-Token": _ITOKEN}, params={"tenant": "globex"})
    assert r.status_code == 404


def test_internal_signed_url_ignores_a_caller_supplied_ttl():
    # A link is a bearer credential; the lifetime is CONFIG, never caller-chosen.
    c = _internal_app()
    fid = _upload_as(c, "acme")
    r = c.post(f"/internal/files/{fid}/signed-url",
               headers={"X-Internal-Token": _ITOKEN}, params={"tenant": "acme", "ttl": 999999})
    assert r.status_code == 200
    from app.config import get_settings
    assert r.json()["expires_in"] == get_settings().SIGNED_URL_TTL_SECONDS


def test_the_signed_token_carries_an_EXPIRY_and_is_tamper_evident():
    from app.adapters.signed_url.hmac_signer import HmacSigner

    signer = HmacSigner(secret="s" * 32, ttl_seconds=300)
    token = signer.sign("file_1")
    assert signer.verify(token)["key"] == "file_1"

    msg, sig = token.split(".", 1)
    assert signer.verify(f"{msg}.{'a' * len(sig)}") is None      # forged signature refused


def test_an_expired_token_is_refused():
    from app.adapters.signed_url.hmac_signer import HmacSigner

    signer = HmacSigner(secret="s" * 32, ttl_seconds=-1)
    assert signer.verify(signer.sign("file_1")) is None


# ── the coupling the guard depends on ─────────────────────────────────────────
def test_an_empty_tenant_claim_normalises_to_None():
    """The guard's `not principal.tenant` and this normalisation are one rule split across two
    files. Mutation-testing showed the guard also holds if written `f.tenant != principal.tenant`
    — but ONLY because a token claiming `tenant: ""` can never produce a Principal with `""`.
    Were that to change, empty-would-equal-empty and an untenanted file would be readable again.
    So the coupling is asserted here rather than left as a thing the guard happens to rely on.
    """
    from app.api.auth import _extract_principal

    for claim in ("", None, 0):
        assert _extract_principal({"sub": "u", "tenant": claim}).tenant is None
    assert _extract_principal({"sub": "u", "tenant": "acme"}).tenant == "acme"
