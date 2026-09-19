"""AAD binding for operator credentials (safety:aesgcm-aad).

The seal binds each ciphertext to its (collection, tenant, name) slot. A DB tamperer with NO KEK
must not be able to copy tenant A's sealed blob into tenant B's row, or under another name, and have
it decrypt — that would let B authenticate as A. A relocated/renamed blob fails the GCM tag.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.secret_store import SecretError, SecretStore

KEK = "k" * 40
SECRET = "sk-live-abcdefghijklmnop3f2a"
_COLL = "operator_secrets"


def run(c):
    return asyncio.run(c)


def _store():
    return SecretStore(MemoryDataStore(), KEK)


def test_a_blob_relocated_to_another_tenant_fails_the_tag():
    st = _store()
    run(st.set(tenant="acme", name="sms_key", value=SECRET))
    row = run(st._s.get(_COLL, "acme|sms_key"))

    # A tamperer copies acme's sealed blob into another tenant's row (no KEK needed for the copy).
    forged = {**row, "id": "evil|sms_key", "tenant": "evil"}
    run(st._s.put(_COLL, "evil|sms_key", forged))
    with pytest.raises(SecretError):
        run(st.get(tenant="evil", name="sms_key"))


def test_a_blob_relocated_under_another_name_fails_the_tag():
    st = _store()
    run(st.set(tenant="acme", name="sms_key", value=SECRET))
    row = run(st._s.get(_COLL, "acme|sms_key"))

    forged = {**row, "id": "acme|other", "name": "other"}
    run(st._s.put(_COLL, "acme|other", forged))
    with pytest.raises(SecretError):
        run(st.get(tenant="acme", name="other"))


def test_the_credential_still_reads_in_its_own_slot():
    """The binding must not break the happy path."""
    st = _store()
    run(st.set(tenant="acme", name="sms_key", value=SECRET))
    assert run(st.get(tenant="acme", name="sms_key")) == SECRET
