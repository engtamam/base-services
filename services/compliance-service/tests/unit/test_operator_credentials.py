"""Resolving a credential an operator may have replaced since boot.

What matters is the ORDER — stored beats configured — and that the cache is a cache rather than a
permanent capture, which is exactly the bug this replaces.
"""
from __future__ import annotations

import asyncio

from app.core.operator_credentials import OperatorCredentials


def run(coro):
    return asyncio.run(coro)


class _Store:
    """Enough of SecretStore to answer get(). Counts reads, so a cache that never expires and a
    cache that never caches are both visible."""

    def __init__(self, values: dict | None = None, raises: bool = False) -> None:
        self.values = values or {}
        self.raises = raises
        self.reads = 0

    async def get(self, *, tenant: str, name: str, fallback: str = "") -> str:
        self.reads += 1
        if self.raises:
            raise ValueError("undecryptable")
        return self.values.get((tenant, name), fallback)


def test_a_stored_credential_beats_the_configured_one():
    creds = OperatorCredentials(_Store({("", "ACME_TOKEN"): "stored-1"}))
    assert run(creds.get("ACME_TOKEN", fallback="from-env")) == "stored-1"


def test_config_is_the_fallback_so_an_unmigrated_deployment_is_unchanged():
    assert run(OperatorCredentials(_Store()).get("ACME_TOKEN", fallback="from-env")) == "from-env"


def test_no_store_at_all_resolves_the_configured_value():
    # No KEK: the operator surface 503s, but resolution still works exactly as it did before.
    assert run(OperatorCredentials(None).get("ACME_TOKEN", fallback="from-env")) == "from-env"


def test_the_cache_expires_rather_than_capturing_permanently():
    store = _Store({("", "ACME_TOKEN"): "old"})
    clock = [0.0]
    creds = OperatorCredentials(store, ttl_s=30, now=lambda: clock[0])
    assert run(creds.get("ACME_TOKEN")) == "old"
    store.values[("", "ACME_TOKEN")] = "new"
    assert run(creds.get("ACME_TOKEN")) == "old", "read through a live cache"
    clock[0] = 31
    assert run(creds.get("ACME_TOKEN")) == "new", "the value was captured, not cached"


def test_invalidate_makes_a_panel_change_visible_immediately():
    store = _Store({("", "ACME_TOKEN"): "old"})
    creds = OperatorCredentials(store, ttl_s=3600)
    run(creds.get("ACME_TOKEN"))
    store.values[("", "ACME_TOKEN")] = "new"
    creds.invalidate()
    assert run(creds.get("ACME_TOKEN")) == "new", "invalidate was a no-op"


def test_a_tenant_credential_does_not_leak_across_tenants():
    store = _Store({("acme", "ACME_TOKEN"): "acme-one", ("globex", "ACME_TOKEN"): "globex-two"})
    creds = OperatorCredentials(store)
    assert run(creds.get("ACME_TOKEN", tenant="acme")) == "acme-one"
    assert run(creds.get("ACME_TOKEN", tenant="globex")) == "globex-two"


def test_an_unreadable_credential_falls_back_rather_than_failing_every_erasure():
    # A broken row is an operator problem; a DSAR that misses its statutory deadline is not.
    assert run(OperatorCredentials(_Store(raises=True)).get(
        "ACME_TOKEN", fallback="from-env")) == "from-env"
