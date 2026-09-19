"""AAD binding for operator-managed credentials (safety:aesgcm-aad).

The KEK is one per deployment, so without associated data a DB tamperer with no KEK could copy one
tenant's sealed blob into another tenant's row (or under another name) and it would still decrypt —
one tenant authenticating as another. AAD = collection|tenant|name binds a blob to its exact slot,
so a relocated or renamed blob fails the GCM tag.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.secret_store import SecretError, SecretStore

KEK = "k" * 40
SECRET = "sk-live-abcdefghijklmnop3f2a"


def run(c):
    return asyncio.run(c)


def test_a_sealed_credential_cannot_be_relocated_to_another_tenant_or_name():
    st = SecretStore(MemoryDataStore(), KEK)
    run(st.set(tenant="acme", name="sms_key", value=SECRET))
    assert run(st.get(tenant="acme", name="sms_key")) == SECRET   # its own slot opens
    row = run(st._s.get("operator_secrets", "acme|sms_key"))

    # Copy the sealed blob into another TENANT's row → open must fail under that tenant's AAD.
    run(st._s.put("operator_secrets", "beta|sms_key",
                  {**row, "id": "beta|sms_key", "tenant": "beta"}))
    with pytest.raises(SecretError):
        run(st.get(tenant="beta", name="sms_key"))

    # Copy the sealed blob under another NAME → open must fail under that name's AAD.
    run(st._s.put("operator_secrets", "acme|other",
                  {**row, "id": "acme|other", "name": "other"}))
    with pytest.raises(SecretError):
        run(st.get(tenant="acme", name="other"))
