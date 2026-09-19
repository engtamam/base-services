"""AEAD associated-data binding (safety:aesgcm-aad).

The backup key is shared fleet-wide, so a sealed archive body authenticates its ciphertext but not
where it lives. Without AAD a tamperer with NO key could relocate a sealed body across
services/tenants and it would still decrypt. This proves a relocated body now fails the GCM tag.

(wallet-service has no operator secret_store, so only the backup archive binding is exercised here.)
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core import backup as B


def run(c):
    return asyncio.run(c)


def test_a_sealed_archive_cannot_be_relocated_to_another_service_or_tenant():
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
