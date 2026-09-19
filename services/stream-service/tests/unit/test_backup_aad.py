"""safety:aesgcm-aad — a sealed archive is bound to its (service, tenant) identity.

BACKUP_KEK is shared fleet-wide, so an archive body sealed with no AAD decrypts under the key inside
ANY envelope: a tamperer could wrap service X's (or tenant A's) sealed body in a service-Y (tenant-B)
envelope and restore Y from X's data. The AAD binds the body to the CLEAR envelope's identity, so a
relocated/renamed archive fails the GCM tag loudly instead.
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


def test_an_honest_archive_still_restores():
    """AAD must not break the legitimate path: an envelope whose identity matches still opens."""
    seal, unseal = B.make_sealer("shared-backup-key")
    body = json.dumps({"service": "svc-A", "tenant": "acme",
                       "collections": {"widgets": [{"id": "w1", "tenant": "acme"}]}})
    sealed = seal(body, aad=B._archive_aad("svc-A", "acme"))
    archive = {"format": B.ARCHIVE_FORMAT, "service": "svc-A", "tenant": "acme", "sealed": sealed}
    archive["checksum"] = B._checksum(sealed)
    report = run(B.import_archive(MemoryDataStore(), archive, unseal=unseal))
    assert report["inserted"] == 1
