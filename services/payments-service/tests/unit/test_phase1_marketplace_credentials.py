"""Phase 1 — the platform-scoped MARKETPLACE_* credential store.

Mirror image of the per-tenant MERCHANT_* store, tested adversarially: cryptographic isolation from the
merchant store (distinct salt → distinct key), platform-scoped resolution, fail-closed `configured`,
SSRF base dropped on read, write-only (no plaintext ever surfaced), and AAD slot-binding so a relocated
blob will not decrypt.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.marketplace_credentials import (API_BASE, PROVIDER, SECRET_KEY, SETTABLE_NAMES,
                                              WEBHOOK_SECRET, MARKETPLACE_COLLECTION,
                                              MarketplaceCredentials, marketplace_secret_store)
from app.core.merchant_credentials import merchant_secret_store, MERCHANT_COLLECTION
from app.core.secret_store import SecretError

KEK = "k" * 40


def run(c):
    return asyncio.run(c)


def _store():
    ds = MemoryDataStore()
    return ds, marketplace_secret_store(ds, KEK)


# ── settable names ───────────────────────────────────────────────────────────────
def test_settable_names_are_the_four_marketplace_keys():
    assert set(SETTABLE_NAMES) == {PROVIDER, SECRET_KEY, API_BASE, WEBHOOK_SECRET}


# ── platform-scoped resolution ───────────────────────────────────────────────────
def test_provider_and_config_resolve_platform_scoped():
    _, s = _store()
    run(s.set(tenant="", name=PROVIDER, value="Tap"))
    run(s.set(tenant="", name=SECRET_KEY, value="sk_live_xyz"))
    creds = MarketplaceCredentials(s)
    assert run(creds.provider()) == "tap"                      # normalised lower
    cfg = run(creds.config())
    assert cfg.provider == "tap" and cfg.secret_key == "sk_live_xyz"


def test_unconfigured_is_fail_closed():
    _, s = _store()
    creds = MarketplaceCredentials(s)
    assert run(creds.configured()) is False
    run(s.set(tenant="", name=PROVIDER, value="tap"))
    assert run(creds.configured()) is False                    # provider but NO secret key → not ready
    run(s.set(tenant="", name=SECRET_KEY, value="sk"))
    assert run(creds.configured()) is True


def test_a_per_tenant_marketplace_value_is_not_inherited():
    # The store is platform-scoped; a stray per-tenant row must not leak into the platform read.
    _, s = _store()
    run(s.set(tenant="acme", name=SECRET_KEY, value="tenant-key"))   # a stray/hostile per-tenant write
    cfg = run(MarketplaceCredentials(s).config())
    assert cfg.secret_key == ""                                # platform read ("") sees nothing


# ── SSRF base dropped on read ─────────────────────────────────────────────────────
@pytest.mark.parametrize("bad_base", [
    "http://provider.example",            # not https
    "https://169.254.169.254/latest",     # cloud metadata IP literal
    "https://127.0.0.1/api",              # loopback IP literal
    "https://10.0.0.5/api",               # private-range IP literal
])
def test_non_public_base_is_dropped_on_read(bad_base):
    # read-time guard drops non-https + private/loopback IP LITERALS; a hostname (e.g. localhost) is
    # caught later by the fetch-time DNS guard, by design (validate_gateway_base does no DNS).
    _, s = _store()
    run(s.set(tenant="", name=PROVIDER, value="tap"))
    run(s.set(tenant="", name=SECRET_KEY, value="sk"))
    run(s.set(tenant="", name=API_BASE, value=bad_base))       # bypasses the write guard (direct set)
    cfg = run(MarketplaceCredentials(s).config())
    assert cfg.base == ""                                      # dropped → adapter uses its safe default


def test_a_public_https_base_is_kept():
    _, s = _store()
    run(s.set(tenant="", name=API_BASE, value="https://api.tap.company/v2"))
    assert run(MarketplaceCredentials(s).config()).base == "https://api.tap.company/v2"


# ── cryptographic isolation from the merchant store ───────────────────────────────
def test_marketplace_and_merchant_stores_are_isolated():
    ds = MemoryDataStore()
    mkt = marketplace_secret_store(ds, KEK)
    mer = merchant_secret_store(ds, KEK)
    run(mkt.set(tenant="", name=SECRET_KEY, value="platform-marketplace-key"))
    run(mer.set(tenant="acme", name="MERCHANT_API_KEY", value="shop-key"))
    # Different collections — one store never sees the other's rows.
    assert MARKETPLACE_COLLECTION != MERCHANT_COLLECTION
    assert run(mkt.get(tenant="", name="MERCHANT_API_KEY", fallback="∅")) == "∅"
    assert run(mer.get(tenant="acme", name=SECRET_KEY, fallback="∅")) == "∅"


def test_a_merchant_blob_cannot_be_decrypted_by_the_marketplace_key():
    # Distinct scrypt salt → distinct derived key. Even if a DB tamperer copied a merchant ciphertext
    # into the marketplace collection, it fails the GCM tag (wrong key AND wrong AAD) — loud, not silent.
    ds = MemoryDataStore()
    mkt = marketplace_secret_store(ds, KEK)
    mer = merchant_secret_store(ds, KEK)
    # The merchant store requires a tenant; seal under one, then relocate its blob into the marketplace
    # collection to simulate a DB tamperer moving ciphertext across stores.
    run(mer.set(tenant="acme", name="X", value="secret"))
    row = run(ds.get(MERCHANT_COLLECTION, "acme|X"))
    # Plant the merchant ciphertext into the marketplace collection under a marketplace key slot.
    run(ds.put(MARKETPLACE_COLLECTION, mkt._key_of("", SECRET_KEY),
               {"id": mkt._key_of("", SECRET_KEY), "tenant": "", "name": SECRET_KEY,
                "sealed": row["sealed"]}))
    with pytest.raises(SecretError):
        run(mkt.get(tenant="", name=SECRET_KEY))


# ── write-only: no plaintext ever surfaces ────────────────────────────────────────
def test_info_and_list_never_expose_the_value():
    _, s = _store()
    run(s.set(tenant="", name=SECRET_KEY, value="sk_live_supersecret_1234"))
    info = run(s.info(tenant="", name=SECRET_KEY))
    assert "sk_live_supersecret_1234" not in (info.hint or "")
    assert info.hint.startswith("•")
    for i in run(s.list_info(tenant="")):
        assert "supersecret" not in (i.hint or "")


# ── rotation: previous secret accepted during the window (SecretStore.accepted) ───
def test_webhook_secret_rotation_keeps_the_previous_during_the_window():
    _, s = _store()
    run(s.set(tenant="", name=WEBHOOK_SECRET, value="old"))
    run(s.set(tenant="", name=WEBHOOK_SECRET, value="new"))
    accepted = run(s.accepted(tenant="", name=WEBHOOK_SECRET))
    assert "new" in accepted and "old" in accepted            # both valid mid-rotation
