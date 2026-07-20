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
def test_the_internal_endpoint_requires_the_tenant_to_MATCH():
    """The internal token is held by every service. Without this check it would be a read
    capability over every tenant's files."""
    import inspect

    from app.api import internal

    src = inspect.getsource(internal.internal_signed_url)
    assert 'if not f.tenant or f.tenant != tenant:' in src
    assert "NOT_FOUND" in src


def test_the_internal_endpoint_does_not_take_a_caller_supplied_TTL():
    """A link is a bearer credential; a caller-chosen lifetime lets a job mint one that outlives
    the reason it was issued."""
    import inspect

    from app.api import internal

    sig = inspect.signature(internal.internal_signed_url)
    assert "ttl" not in sig.parameters and "expires_in" not in sig.parameters
    assert "SIGNED_URL_TTL_SECONDS" in inspect.getsource(internal.internal_signed_url)


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
