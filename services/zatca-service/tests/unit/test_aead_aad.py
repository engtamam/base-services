"""AEAD associated-data binding (safety:aesgcm-aad).

Three sealed surfaces in this service must bind their ciphertext to its slot, so a tamperer with NO
key cannot relocate a sealed blob and still have it decrypt:
  - backup archives (BACKUP_KEK, shared fleet-wide) — bound to (service, tenant);
  - operator credentials (secret_store) — bound to (collection, tenant, name);
  - CSID material at rest (seal.py, per-service key) — bound to (collection, tenant, field).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core import backup as B
from app.core.seal import seal as csid_seal
from app.core.seal import unseal as csid_unseal
from app.core.secret_store import SecretError, SecretStore, _COLL
from app.core.stores import _SETTINGS, SettingsStore, _csid_aad


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


def test_a_sealed_credential_cannot_be_relocated_to_another_row():
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


def test_csid_seal_is_bound_to_its_slot():
    """A relocated/renamed CSID blob fails the tag (unseal returns None), so a tamperer cannot copy
    tenant A's compliance cert into tenant B's row, nor swap the cert blob into the key field."""
    blob = csid_seal("cert-pem", "seal-key", _csid_aad("acme", "csid_cert"))
    # Same key, right slot → round-trips.
    assert csid_unseal(blob, "seal-key", _csid_aad("acme", "csid_cert")) == "cert-pem"
    # Relocated to another tenant → fails.
    assert csid_unseal(blob, "seal-key", _csid_aad("globex", "csid_cert")) is None
    # Swapped into another field → fails.
    assert csid_unseal(blob, "seal-key", _csid_aad("acme", "private_key")) is None


def test_relocating_a_whole_csid_settings_row_fails_closed():
    """load_secrets on a settings row copied into another tenant must not return the original's
    material — the AAD mismatch makes the unseals fail, so it returns None (fails closed)."""
    store = MemoryDataStore()
    ss = SettingsStore(store, "seal-key")
    run(ss.store_csid("acme", certificate_pem="cert-pem", secret="sec",
                      private_key_pem="key-pem"))
    acme_row = run(store.get(_SETTINGS, "acme"))
    forged = {**acme_row, "id": "globex"}
    run(store.put(_SETTINGS, "globex", forged))
    assert run(ss.load_secrets("globex")) is None
    # The legitimate row still loads under its own tenant.
    assert run(ss.load_secrets("acme"))["certificate_pem"] == "cert-pem"
