"""Reads of sealed operator values, kept OUT of the API layer.

`SecretStore.get()` returns plaintext, and the whole write-only guarantee rests on that call never
sitting in an HTTP handler — one stray `return` beside it leaks every tenant's value
(safety:no-secret-reads). So the reads live here, in core, and a route calls these helpers instead of
the store directly.

Two shapes:
  * read_override — a NON-secret runtime override (e.g. the public Meta app id / login config id,
    which reach the browser by design). Returned to the caller because it is meant to be.
  * load_private_key — a sealed PRIVATE KEY consumed in-process (the Flow data-endpoint decrypt).
    Loaded here so the route never issues the plaintext read; the key is used by the crypto layer.
"""
from __future__ import annotations


async def read_override(store, *, tenant: str, name: str, fallback: str = "") -> str:
    """The effective value of a runtime override: the sealed store's value, else the fallback. A store
    hiccup falls back rather than failing the request. `store` is duck-typed to the operator
    SecretStore (async `get(tenant, name, fallback)`); None ⇒ the fallback."""
    if store is None:
        return fallback
    try:
        return await store.get(tenant=tenant, name=name, fallback=fallback)
    except Exception:  # noqa: BLE001 — a store hiccup falls back to the configured value
        return fallback


async def load_private_key(store, *, tenant: str, key_name: str,
                           passphrase_name: str = "") -> tuple[str, bytes | None]:
    """Load a sealed private key (and optional passphrase) for in-process crypto — the Flow
    data-endpoint decrypt. Returns ("", None) when unconfigured, so the caller can answer a clean
    503 rather than crash. The plaintext read is here, never in the route."""
    if store is None:
        return "", None
    private_pem = await read_override(store, tenant=tenant, name=key_name, fallback="")
    passphrase: bytes | None = None
    if passphrase_name:
        pp = await read_override(store, tenant=tenant, name=passphrase_name, fallback="")
        passphrase = pp.encode("utf-8") if pp else None
    return private_pem, passphrase
