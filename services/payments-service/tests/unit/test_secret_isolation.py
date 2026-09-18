"""Isolation & tamper properties of the secret stores (audit H4, M1, L1).

Each test here fails against the pre-fix code:
  * the merchant store must NOT inherit the platform-wide ("*") row, while the operator store still
    does (H4);
  * a ciphertext copied into a different tenant/name row must fail to open, not decrypt into the
    wrong identity (M1 — AAD binding);
  * a short secret's hint must reveal no tail (L1);
  * a platform-wide ("") merchant write must be refused outright (H4, belt-and-braces).
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.merchant_credentials import MERCHANT_COLLECTION, merchant_secret_store
from app.core.secret_store import SecretError, SecretStore

KEK = "k" * 40


def run(c):
    return asyncio.run(c)


# ── H4: no cross-tenant fallback for the merchant store ─────────────────────────
def test_merchant_store_does_not_inherit_the_platform_wide_row():
    """A stray "*|MERCHANT_*" row (e.g. a backup restore) must NOT be inherited by every tenant —
    money would route to the wrong shop. The merchant store resolves strictly to (tenant, name)."""
    store = MemoryDataStore()
    # Land a platform-wide row directly (the write path refuses tenant="" — see below — so we plant
    # it as a restore/remap would, straight into the datastore under the "*" key).
    op = SecretStore(store, KEK, collection=MERCHANT_COLLECTION, salt=b"merchant-payment-secrets-v1")
    # op has platform_fallback on but require_tenant off — use it only to seal a well-formed "" row.
    run(op.set(tenant="", name="MERCHANT_API_KEY", value="platform-merchant-key"))

    merchant = merchant_secret_store(store, KEK)
    got = run(merchant.get(tenant="acme", name="MERCHANT_API_KEY", fallback="fell-back"))
    assert got == "fell-back", "the merchant store inherited a platform-wide credential"


def test_operator_store_still_inherits_the_platform_wide_row():
    """The operator store's platform default behaviour is preserved exactly (contrast with above)."""
    store = MemoryDataStore()
    op = SecretStore(store, KEK)  # defaults: platform_fallback=True
    run(op.set(tenant="", name="sms_key", value="platform-wide"))
    assert run(op.get(tenant="acme", name="sms_key")) == "platform-wide"


def test_a_platform_wide_merchant_write_is_refused():
    store = MemoryDataStore()
    merchant = merchant_secret_store(store, KEK)
    with pytest.raises(SecretError, match="tenant"):
        run(merchant.set(tenant="", name="MERCHANT_API_KEY", value="x"))


# ── M1: AAD binds a ciphertext to its exact (collection, tenant, name) slot ─────
def test_a_relocated_ciphertext_fails_to_open():
    """A DB tamperer (no KEK) copies tenant A's sealed blob into tenant B's row. It must fail the GCM
    tag, not decrypt — otherwise B authenticates as A."""
    store = MemoryDataStore()
    st = SecretStore(store, KEK)
    run(st.set(tenant="A", name="sms_key", value="A-secret-value-1234"))

    a_row = run(store.get(st._coll, "A|sms_key"))
    # Plant B's row reusing A's ciphertext verbatim (relocation across tenant).
    run(store.put(st._coll, "B|sms_key",
                  {"id": "B|sms_key", "tenant": "B", "name": "sms_key",
                   "sealed": a_row["sealed"], "hint": a_row["hint"]}))
    with pytest.raises(SecretError):
        run(st.get(tenant="B", name="sms_key"))


def test_a_renamed_ciphertext_fails_to_open():
    """Same blob copied under a different `name` must also fail — AAD binds the name too."""
    store = MemoryDataStore()
    st = SecretStore(store, KEK)
    run(st.set(tenant="A", name="sms_key", value="A-secret-value-1234"))
    a_row = run(store.get(st._coll, "A|sms_key"))
    run(store.put(st._coll, "A|other_key",
                  {"id": "A|other_key", "tenant": "A", "name": "other_key",
                   "sealed": a_row["sealed"], "hint": a_row["hint"]}))
    with pytest.raises(SecretError):
        run(st.get(tenant="A", name="other_key"))


def test_an_honest_round_trip_still_opens_and_rotation_survives():
    """AAD must not break the legitimate paths: read-back and previous-during-window both still work."""
    clock = {"t": 1000.0}
    st = SecretStore(MemoryDataStore(), KEK, now=lambda: clock["t"], rotation_window_s=300)
    run(st.set(tenant="acme", name="webhook_secret", value="old-value-abcdef"))
    run(st.set(tenant="acme", name="webhook_secret", value="new-value-abcdef"))
    assert run(st.get(tenant="acme", name="webhook_secret")) == "new-value-abcdef"
    assert set(run(st.accepted(tenant="acme", name="webhook_secret"))) == {
        "old-value-abcdef", "new-value-abcdef"}


# ── L1: a short secret's hint reveals no tail ──────────────────────────────────
def test_a_short_secret_reveals_no_tail_in_its_hint():
    st = SecretStore(MemoryDataStore(), KEK)
    info = run(st.set(tenant="acme", name="k", value="short12"))  # 7 chars < 12
    assert info.hint == "•" * 4, "a short secret leaked a tail"
    assert "short12" not in info.hint and "rt12" not in info.hint


def test_a_just_under_threshold_secret_reveals_no_tail():
    st = SecretStore(MemoryDataStore(), KEK)
    info = run(st.set(tenant="acme", name="k", value="a" * 11))  # 11 < 12
    assert info.hint == "•" * 4


def test_a_long_secret_still_reveals_its_last_four():
    st = SecretStore(MemoryDataStore(), KEK)
    info = run(st.set(tenant="acme", name="k", value="sk-live-abcdefgh3f2a"))  # >= 12
    assert info.hint.endswith("3f2a")
