"""AEAD associated-data binding (safety:aesgcm-aad).

A sealed blob must be bound to its identity, so a blob relocated to another service/tenant/row fails
the GCM tag LOUDLY instead of decrypting. BACKUP_KEK (and SECRETS_KEK) derive one key that is the
same everywhere it is used, so WITHOUT this binding a sealed body could be moved and would still
decrypt — restoring one identity from another's data. These tests prove the move now fails.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core import backup as B
from app.core.secret_store import SecretError, SecretStore


def run(c):
    return asyncio.run(c)


def test_a_sealed_archive_cannot_be_relocated_to_another_service_or_tenant():
    """BACKUP_KEK is shared fleet-wide: a body sealed for one service/tenant must NOT decrypt when
    wrapped in another's envelope, or a tamperer could restore service Y (tenant B) from X's (A's)
    archive under the same key."""
    seal, unseal = B.make_sealer("shared-backup-key")
    body = json.dumps({"service": "svc-A", "tenant": "acme", "collections": {}})
    sealed = seal(body, aad=B._archive_aad("svc-A", "acme"))

    # Same key, forged SERVICE in the clear envelope → unseal must fail the GCM tag.
    forged_service = {"format": B.ARCHIVE_FORMAT, "service": "svc-B", "tenant": "acme",
                      "sealed": sealed}
    forged_service["checksum"] = B._checksum(sealed)
    with pytest.raises(B.BackupError):
        run(B.import_archive(MemoryDataStore(), forged_service, unseal=unseal))

    # Same key, forged TENANT → unseal must fail.
    forged_tenant = {"format": B.ARCHIVE_FORMAT, "service": "svc-A", "tenant": "evil",
                     "sealed": sealed}
    forged_tenant["checksum"] = B._checksum(sealed)
    with pytest.raises(B.BackupError):
        run(B.import_archive(MemoryDataStore(), forged_tenant, unseal=unseal))


def test_a_credential_blob_cannot_be_relocated_across_rows():
    """A DB tamperer (no KEK) who copies tenant A's sealed blob into tenant B's row must not get a
    working credential — the AAD (collection|tenant|name) makes a relocated blob fail the GCM tag."""
    kek = "k" * 40
    st = MemoryDataStore()
    store = SecretStore(st, kek)
    run(store.set(tenant="acme", name="sms_key", value="sk-live-secret-value"))
    assert run(store.get(tenant="acme", name="sms_key")) == "sk-live-secret-value"

    # Relocate acme's sealed blob into globex's row, as a DB-only tamperer could.
    acme_row = run(st.get("operator_secrets", "acme|sms_key"))
    run(st.put("operator_secrets", "globex|sms_key",
               {**acme_row, "id": "globex|sms_key", "tenant": "globex"}))
    with pytest.raises(SecretError):
        run(store.get(tenant="globex", name="sms_key"))
