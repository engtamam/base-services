"""H5 — the scrypt KEK is derived once per (kek,salt), not per SecretStore construction."""
from __future__ import annotations

from unittest.mock import patch

from app.adapters.datastore.memory import MemoryDataStore
from app.core import secret_store as ss
from app.core.merchant_credentials import merchant_secret_store


def test_kek_derivation_is_memoized_across_constructions():
    ss._DERIVED_KEY_CACHE.clear()
    store = MemoryDataStore()
    with patch.object(ss.hashlib, "scrypt", wraps=ss.hashlib.scrypt) as spy:
        for _ in range(20):                       # 20 per-request-style constructions
            merchant_secret_store(store, "k" * 40)
        assert spy.call_count == 1                # scrypt ran ONCE, not 20 times
    # a different salt (the operator store) derives its own key once more
    with patch.object(ss.hashlib, "scrypt", wraps=ss.hashlib.scrypt) as spy2:
        ss.SecretStore(store, "k" * 40)           # default operator salt
        ss.SecretStore(store, "k" * 40)
        assert spy2.call_count == 1
