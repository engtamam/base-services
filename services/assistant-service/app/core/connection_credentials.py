"""Per-connection credential resolution — the plaintext read, kept OUT of the API layer.

An AI connection authenticates with its OWN secret: an API key sent as a Bearer, or a Google service-
account JSON minted into a short-lived Vertex OAuth token when auth='vertex'. Resolving that
credential means reading the sealed secret's plaintext (`SecretStore.get`) — and that read must not
live in an HTTP handler. One stray `return` beside it would hand every tenant's provider key to
whoever holds the panel capability (safety:no-secret-reads). So the resolver is built here, in core;
the API layer only wires it into the model adapter and never sees the value.

`store` is duck-typed to the operator SecretStore (async `get(tenant, name, fallback)`); the private
key/JSON never leaves this module — for Vertex it is exchanged for a token here, and only the token
(or, for an API key, the key) is handed to the adapter as the request credential.
"""
from __future__ import annotations

import hashlib

from app.core.gcp_token import VertexTokenSource


def conn_secret_name(cid: str) -> str:
    """The SecretStore name a connection's credential is sealed under."""
    return f"AI_CONNECTION_KEY:{cid}"


def vertex_source(cache: dict, cid: str, secret: str) -> VertexTokenSource:
    """One cached VertexTokenSource per connection, so its short-lived-token cache actually survives
    between calls. Keyed by connection id and rebuilt only when the secret rotates (a sha256
    fingerprint mismatch), so `cache` holds at most one live source per connection. `cache` is a
    plain dict owned by the caller (e.g. app.state), passed in rather than reached for, so this stays
    free of the request object."""
    fp = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    prev = cache.get(cid)
    if prev is None or prev[0] != fp:
        src = VertexTokenSource(sa_json=secret)
        cache[cid] = (fp, src)
        return src
    return prev[1]


def build_key_resolver(store, conn, vertex_cache: dict):
    """An async () -> str resolver that yields THIS connection's request credential: the Vertex OAuth
    token (auth='vertex') or the raw API key. The sealed-secret plaintext read happens here, in core,
    never in a route."""
    async def resolver() -> str:
        secret = await store.get(tenant=conn.tenant, name=conn_secret_name(conn.id),
                                 fallback="") if store is not None else ""
        if conn.auth == "vertex":
            return await vertex_source(vertex_cache, conn.id, secret).token()
        return secret
    return resolver
