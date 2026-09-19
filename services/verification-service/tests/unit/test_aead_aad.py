"""AEAD associated-data binding (safety:aesgcm-aad).

A sealed blob authenticates its ciphertext but NOT where it lives. The backup key is shared
fleet-wide and the secret-store key is one per service, so without AAD a tamperer with NO key could
relocate a sealed blob across services/tenants/rows and it would still decrypt. These tests prove a
relocated or renamed blob now fails the GCM tag loudly.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core import backup as B
from app.core.secret_store import SecretError, SecretStore, _COLL


def run(c):
    return asyncio.run(c)


def test_a_sealed_archive_cannot_be_relocated_to_another_service_or_tenant():
    """BACKUP_KEK is shared fleet-wide, so a body sealed for one service/tenant must NOT decrypt when
    wrapped in another's envelope — else a tamperer could restore service Y (or tenant B) from
    service X's (tenant A's) archive."""
    seal, unseal = B.make_sealer("shared-backup-key")
    body = json.dumps({"service": "svc-A", "tenant": "acme", "collections": {}})
    sealed = seal(body, aad=B._archive_aad("svc-A", "acme"))

    forged_service = {"format": B.ARCHIVE_FORMAT, "service": "svc-B", "tenant": "acme",
                      "sealed": sealed}
    forged_service["checksum"] = B._checksum(sealed)
    with pytest.raises(B.BackupError):
        run(B.import_archive(MemoryDataStore(), forged_service, unseal=unseal))

    forged_tenant = {"format": B.ARCHIVE_FORMAT, "service": "svc-A", "tenant": "evil",
                     "sealed": sealed}
    forged_tenant["checksum"] = B._checksum(sealed)
    with pytest.raises(B.BackupError):
        run(B.import_archive(MemoryDataStore(), forged_tenant, unseal=unseal))


def test_a_sealed_credential_cannot_be_relocated_to_another_row():
    """AAD = collection|tenant|name, so a DB tamperer (no KEK) that copies tenant A's sealed blob into
    tenant B's row, or renames it, gets a tag failure instead of B authenticating as A."""
    st = SecretStore(MemoryDataStore(), "k" * 40)
    secret = "sk-live-abcdefghijklmnop3f2a"
    run(st.set(tenant="acme", name="sms_key", value=secret))

    acme_row = run(st._s.get(_COLL, "acme|sms_key"))
    forged = {**acme_row, "id": "globex|sms_key", "tenant": "globex"}
    run(st._s.put(_COLL, "globex|sms_key", forged))
    with pytest.raises(SecretError):
        run(st.get(tenant="globex", name="sms_key"))

    forged_name = {**acme_row, "id": "acme|other_key", "name": "other_key"}
    run(st._s.put(_COLL, "acme|other_key", forged_name))
    with pytest.raises(SecretError):
        run(st.get(tenant="acme", name="other_key"))
