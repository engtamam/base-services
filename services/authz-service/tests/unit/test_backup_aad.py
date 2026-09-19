"""AAD binding for backup archives (safety:aesgcm-aad).

BACKUP_KEK is shared fleet-wide, so without associated data a sealed archive body could be relocated
across services/tenants — the same key decrypts it anywhere. The body is bound to the CLEAR
envelope's (service, tenant); a wrapper claiming a different identity fails the AEAD tag on unseal.
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

    # Same key, forged envelope claiming a different SERVICE → unseal must fail.
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
