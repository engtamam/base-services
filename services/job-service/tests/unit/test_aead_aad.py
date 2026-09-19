"""AEAD associated-data binding (safety:aesgcm-aad).

A sealed archive must be bound to its identity, so a body relocated to another service/tenant fails
the GCM tag LOUDLY instead of decrypting. BACKUP_KEK derives one key that is the same everywhere it
is used, so WITHOUT this binding a sealed body could be moved and would still decrypt — restoring one
identity from another's data. (job-service has no operator SecretStore, so only the archive path.)
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
