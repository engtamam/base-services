"""safety:aesgcm-aad — the two AEAD seals in this service bind their ciphertext to its slot.

  * SecretStore (operator credentials) binds AAD = collection|tenant|name, so a DB tamperer cannot
    copy one tenant's sealed credential into another tenant's (or another name's) row.
  * Sealer (source camera URLs) binds AAD = the source id, so a sealed URL cannot be relocated onto
    a different source row under the single shared SEAL_KEY.
Each relocation must fail the GCM tag loudly; each honest round-trip must still open.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.sealing import Sealer, source_aad
from app.core.secret_store import SecretError, SecretStore

KEK = "k" * 40


def run(c):
    return asyncio.run(c)


# ── SecretStore: AAD binds (collection, tenant, name) ───────────────────────────
def test_a_relocated_credential_ciphertext_fails_to_open():
    store = MemoryDataStore()
    st = SecretStore(store, KEK)
    run(st.set(tenant="A", name="sms_key", value="A-secret-value-1234"))

    a_row = run(store.get("operator_secrets", "A|sms_key"))
    # Plant B's row reusing A's ciphertext verbatim (relocation across tenant).
    run(store.put("operator_secrets", "B|sms_key",
                  {"id": "B|sms_key", "tenant": "B", "name": "sms_key",
                   "sealed": a_row["sealed"], "hint": a_row["hint"]}))
    with pytest.raises(SecretError):
        run(st.get(tenant="B", name="sms_key"))


def test_a_renamed_credential_ciphertext_fails_to_open():
    store = MemoryDataStore()
    st = SecretStore(store, KEK)
    run(st.set(tenant="A", name="sms_key", value="A-secret-value-1234"))
    a_row = run(store.get("operator_secrets", "A|sms_key"))
    run(store.put("operator_secrets", "A|other_key",
                  {"id": "A|other_key", "tenant": "A", "name": "other_key",
                   "sealed": a_row["sealed"], "hint": a_row["hint"]}))
    with pytest.raises(SecretError):
        run(st.get(tenant="A", name="other_key"))


def test_an_honest_credential_round_trip_still_opens():
    """AAD must not break the legitimate paths: read-back and previous-during-window both still work."""
    clock = {"t": 1000.0}
    st = SecretStore(MemoryDataStore(), KEK, now=lambda: clock["t"], rotation_window_s=300)
    run(st.set(tenant="acme", name="webhook_secret", value="old-value-abcdef"))
    run(st.set(tenant="acme", name="webhook_secret", value="new-value-abcdef"))
    assert run(st.get(tenant="acme", name="webhook_secret")) == "new-value-abcdef"
    assert set(run(st.accepted(tenant="acme", name="webhook_secret"))) == {
        "old-value-abcdef", "new-value-abcdef"}


# ── Sealer: AAD binds a sealed source URL to its source id ──────────────────────
def test_a_sealed_source_url_cannot_be_relocated_to_another_source():
    s = Sealer("k" * 32)
    sealed = s.seal("rtsp://user:hunter2@cam/1", aad=source_aad("src-1"))
    # The same blob under a different source id must fail the tag, not decrypt.
    with pytest.raises(Exception):
        s.unseal(sealed, aad=source_aad("src-2"))
    # Honest identity still opens.
    assert s.unseal(sealed, aad=source_aad("src-1")) == "rtsp://user:hunter2@cam/1"
