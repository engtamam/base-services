"""The seam where an operator-set OAuth client secret reaches the provider adapter.

The risk this covers is not "does the store work" — it is that the resolution is wired to the code
that actually authenticates. A store nobody reads from looks identical to one that works, right up
until a rotation silently does nothing and every sync starts failing against a secret the provider
no longer accepts.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.adapters.datastore.memory import MemoryDataStore
from app.api.v1.connectors import _spec_for
from app.config import get_settings
from app.core import seal as _seal
from app.core.models import Connection, ProviderSpec
from app.core.secret_store import SecretStore
from app.core.stores import SpecStore

KEK = "unit-test-kek-not-a-real-key"
STORED = "cs_stored_9999"
FROM_SPEC = "cs_from_spec_1111"


def run(c):
    return asyncio.run(c)


async def _fixture(with_kek: bool, *, tenant: str = "t1"):
    ds = MemoryDataStore()
    s = get_settings()
    spec = ProviderSpec(id="spec_1", tenant=tenant, name="crm", client_id="cid",
                        client_secret_sealed=_seal.seal(FROM_SPEC, s.seal_key))
    await SpecStore(ds).save(spec)
    secrets = SecretStore(ds, KEK) if with_kek else None
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        container=SimpleNamespace(datastore=ds), secret_store=secrets)))
    conn = Connection(id="con_1", tenant=tenant, spec_id="spec_1", provider="crm")
    return request, conn, secrets, s


def _name(s, spec_id: str = "spec_1") -> str:
    return f"{s.OPERATOR_SPEC_SECRET_PREFIX}{spec_id}"


def test_nothing_stored_behaves_exactly_as_before():
    """No deployment has to migrate: the spec's own secret is still the answer."""
    async def go():
        request, conn, _, _ = await _fixture(True)
        assert (await _spec_for(request, conn))["client_secret"] == FROM_SPEC
    run(go())


def test_a_stored_credential_wins_over_the_spec():
    """The rotation must reach the adapter, or it is a change that only looks like it happened."""
    async def go():
        request, conn, secrets, s = await _fixture(True)
        await secrets.set(tenant=conn.tenant, name=_name(s), value=STORED)
        assert (await _spec_for(request, conn))["client_secret"] == STORED
    run(go())


def test_another_tenants_credential_is_never_used():
    """A cross-tenant leak here authenticates one customer's sync as another's registered app."""
    async def go():
        request, conn, secrets, s = await _fixture(True)
        await secrets.set(tenant="other", name=_name(s), value=STORED)
        assert (await _spec_for(request, conn))["client_secret"] == FROM_SPEC
    run(go())


def test_without_a_kek_the_spec_secret_still_works():
    """No KEK disables the operator surface; it must not disable connectors."""
    async def go():
        request, conn, _, _ = await _fixture(False)
        assert (await _spec_for(request, conn))["client_secret"] == FROM_SPEC
    run(go())


def test_the_stored_credential_is_not_readable_from_the_row():
    """At rest it is ciphertext. A stolen database alone must not yield the secret."""
    async def go():
        request, conn, secrets, s = await _fixture(True)
        await secrets.set(tenant=conn.tenant, name=_name(s), value=STORED)
        ds = request.app.state.container.datastore
        row = await ds.get("operator_secrets", f"{conn.tenant}|{_name(s)}")
        assert STORED not in str(row)
        assert row["hint"].endswith(STORED[-4:])
    run(go())
