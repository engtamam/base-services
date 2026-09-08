"""L2 — the generic dynamic-Flow lifecycle: decrypt -> (ping | handler) -> encrypt, in one call.

GENERIC: any dynamic Flow (booking, a survey, a dynamic form, ...) is served by this — the domain
lives entirely in the injected `handler`. A platform that hosts its own endpoint can call `process`
with the raw request parts and its own key and get back the raw reply, owning the webhook, keys,
routing and numbers itself (never constrained by us). Booking is just one handler (L4).

`ping` is answered HERE, before the handler and before any DB/provider touch — Meta health-checks
published Flows periodically, and a health check must be cheap and must never depend on the domain.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from app.core import flow_crypto

PING, INIT, BACK, DATA_EXCHANGE = "ping", "INIT", "BACK", "data_exchange"

# The simplest handler shape: (decrypted payload) -> a bare navigation/terminal dict.
Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@runtime_checkable
class FlowHandler(Protocol):
    """What a dynamic-Flow handler implements. `name` lets a registry route to it; `handle` takes the
    decrypted request payload ({version, action, screen, data, flow_token}) and returns the next
    screen ({screen, data}) or the terminal {data:{extension_message_response:{...}}}.

    `tenant` is OPTIONAL context (the shop, resolved by the endpoint however it likes — a URL segment,
    the flow definition). A single-brand handler ignores it; a multi-tenant one scopes on it. Keeping
    it here (not in `process`) means L2 stays free of any routing/topology assumption."""
    name: str

    async def handle(self, payload: dict[str, Any], *, tenant: str = "") -> dict[str, Any]: ...


async def process(*, encrypted_flow_data: str, encrypted_aes_key: str, initial_vector: str,
                  private_key_pem: bytes, handler: Handler,
                  passphrase: bytes | None = None) -> str:
    """Decrypt -> dispatch -> encrypt. Returns the base64 reply body Meta expects. `ping` is answered
    here (cheap, no handler); every other action goes to `handler`. Raises FlowCryptoError on a
    decrypt failure so the caller can answer 421."""
    payload, aes_key, iv = flow_crypto.decrypt_request(
        encrypted_flow_data=encrypted_flow_data, encrypted_aes_key=encrypted_aes_key,
        initial_vector=initial_vector, private_key_pem=private_key_pem, passphrase=passphrase)

    if payload.get("action") == PING:
        response: dict[str, Any] = {"data": {"status": "active"}}
    else:
        response = await handler(payload)
    return flow_crypto.encrypt_response(response, aes_key=aes_key, iv=iv)
