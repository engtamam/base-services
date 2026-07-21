"""The model credential resolved at runtime, and the cache that keeps it off the hot path.

What these pin down is the reason the feature exists: a key set through the operator surface must
reach the model adapter, and a deployment that has stored nothing must behave exactly as it did
before. A test that only asserted "the store can hold a value" would pass against a build where
the adapter still used the boot-time config string — which is the whole defect.
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.model.http_provider import HttpModel
from app.core.model_credentials import ModelCredentials
from app.core.secret_store import SecretStore
from app.ports.model import Passage


def run(c):
    return asyncio.run(c)


class _Settings:
    MODEL_API_KEY = "from-the-environment"


def _store(kek: str = "k" * 32) -> SecretStore:
    return SecretStore(MemoryDataStore(), kek)


async def _test_configured_key_is_used_when_nothing_is_stored():
    creds = ModelCredentials(_Settings(), _store())
    assert await creds.get() == "from-the-environment"


async def _test_stored_key_beats_the_environment_one():
    store = _store()
    await store.set(tenant="", name="MODEL_API_KEY", value="set-by-the-operator")
    creds = ModelCredentials(_Settings(), store)
    assert await creds.get() == "set-by-the-operator"


async def _test_no_store_falls_back_to_config():
    """No KEK means no store, and answering must be untouched rather than broken."""
    creds = ModelCredentials(_Settings(), None)
    assert await creds.get() == "from-the-environment"


async def _test_resolution_is_cached_then_invalidated():
    """The cache is the point — and an operator's own change must not wait for it."""
    store = _store()
    reads = []
    real_get = store.get

    async def counting_get(**kw):
        reads.append(kw["name"])
        return await real_get(**kw)

    store.get = counting_get                       # type: ignore[method-assign]
    creds = ModelCredentials(_Settings(), store, ttl_s=300)
    await creds.get()
    await creds.get()
    assert len(reads) == 1, "the store was read again on the hot path"

    await store.set(tenant="", name="MODEL_API_KEY", value="rotated")
    assert await creds.get() == "from-the-environment", "a cache that never holds is not a cache"
    creds.invalidate()
    assert await creds.get() == "rotated"


async def _test_unreadable_credential_does_not_break_answering():
    """A row sealed under a different KEK falls back rather than 500ing every question."""
    store = _store()
    await store.set(tenant="", name="MODEL_API_KEY", value="sealed-under-the-old-kek")
    other = SecretStore(store._s, "z" * 32)        # noqa: SLF001 — same rows, wrong key
    creds = ModelCredentials(_Settings(), other)
    assert await creds.get() == "from-the-environment"


async def _test_a_rotation_reaches_the_model_adapter():
    """The load-bearing one: the new key must appear in the outbound Authorization header."""
    store = _store()
    creds = ModelCredentials(_Settings(), store, ttl_s=300)
    seen: list[str] = []

    async def transport(url, headers, payload, timeout):
        seen.append(headers.get("Authorization", ""))
        return 200, {"choices": [{"message": {"content": "an answer [1]"}}]}

    model = HttpModel("https://model.invalid/v1", "from-the-environment", transport=transport)
    model.use_key_resolver(creds.get)
    passages = (Passage(ref="a#0", title="T", text="body"),)

    await model.answer(question="q", passages=passages, lang="en")
    assert seen[-1] == "Bearer from-the-environment"

    await store.set(tenant="", name="MODEL_API_KEY", value="rotated-by-the-operator")
    creds.invalidate()
    await model.answer(question="q", passages=passages, lang="en")
    assert seen[-1] == "Bearer rotated-by-the-operator"


async def _test_adapter_without_a_resolver_is_unchanged():
    """Every existing deployment builds it this way; the migration must be invisible to them."""
    seen: list[str] = []

    async def transport(url, headers, payload, timeout):
        seen.append(headers.get("Authorization", ""))
        return 200, {"choices": [{"message": {"content": "hi [1]"}}]}

    model = HttpModel("https://model.invalid/v1", "plain-config-key", transport=transport)
    await model.answer(question="q", passages=(Passage(ref="a#0", title="T", text="b"),), lang="en")
    assert seen == ["Bearer plain-config-key"]


def test_configured_key_is_used_when_nothing_is_stored():
    run(_test_configured_key_is_used_when_nothing_is_stored())

def test_stored_key_beats_the_environment_one():
    run(_test_stored_key_beats_the_environment_one())

def test_no_store_falls_back_to_config():
    run(_test_no_store_falls_back_to_config())

def test_resolution_is_cached_then_invalidated():
    run(_test_resolution_is_cached_then_invalidated())

def test_unreadable_credential_does_not_break_answering():
    run(_test_unreadable_credential_does_not_break_answering())

def test_a_rotation_reaches_the_model_adapter():
    run(_test_a_rotation_reaches_the_model_adapter())

def test_adapter_without_a_resolver_is_unchanged():
    run(_test_adapter_without_a_resolver_is_unchanged())
