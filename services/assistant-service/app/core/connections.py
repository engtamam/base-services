"""AI connections — N isolated LLM "lines" per tenant, each with its own provider, prompt and price.

Separate from the service's ONE built-in KB assistant. A tenant can declare any number of connections
(the count is theirs, never hardcoded here): each has its own endpoint + model + shape + auth, its own
system prompt, and its own per-1k-token price. They are ISOLATED — invoking one uses only that one's
config + credential, meters only that one's usage, and never touches another connection or the main
model. This is what lets a platform run several AI purposes side by side and price each differently.

Credentials are NOT stored on the connection row — they live in the secret store, write-only, keyed
by the connection id (the route owns that). This module holds the definition, the usage ledger, and
the cost arithmetic; the actual model call is an injected `caller` so core imports no adapter.

Pricing is INTEGER micros per 1000 tokens (1 micro = 1e-6 of the currency unit) — money is never a
float. cost = tokens/1000 * price, summed for input and output.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

_CONNECTIONS = "ai_connections"
_USAGE = "ai_connection_usage"
_SHAPES = ("chat", "generate")
_AUTHS = ("api_key", "vertex")


class ConnectionError(ValueError):
    """A connection definition the service refuses (bad shape/auth/name/price)."""


# caller(system, user, max_tokens) -> (text, tokens_in, tokens_out). Injected so core stays adapter-free.
Caller = Callable[[str, str, int], Awaitable[tuple[str, int, int]]]


@dataclass
class AiConnection:
    id: str
    tenant: str
    name: str
    url: str
    shape: str = "chat"                  # chat | generate
    model: str = ""
    auth: str = "api_key"                # api_key | vertex
    system_prompt: str = ""
    temperature: float = 0.2
    input_price_micros: int = 0          # micros per 1000 input tokens
    output_price_micros: int = 0         # micros per 1000 output tokens
    currency: str = "SAR"
    created: str = ""
    updated: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in (
            "id", "tenant", "name", "url", "shape", "model", "auth", "system_prompt",
            "temperature", "input_price_micros", "output_price_micros", "currency",
            "created", "updated")}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AiConnection":
        return cls(**{k: d.get(k, getattr(cls, k, None)) for k in (
            "id", "tenant", "name", "url", "shape", "model", "auth", "system_prompt",
            "temperature", "input_price_micros", "output_price_micros", "currency",
            "created", "updated")})

    def public(self) -> dict[str, Any]:
        # No secret is ever on the row (the key lives in the secret store), so the whole row is safe.
        return self.to_dict()


def cost_micros(tokens_in: int, tokens_out: int, in_price: int, out_price: int) -> int:
    """Integer micros. Rounded per side so a fractional 1k-token block still bills, never a float."""
    ci = (int(tokens_in) * int(in_price) + 999) // 1000 if tokens_in and in_price else 0
    co = (int(tokens_out) * int(out_price) + 999) // 1000 if tokens_out and out_price else 0
    return ci + co


def _key(tenant: str, cid: str) -> str:
    return f"{tenant}|{cid}"


class ConnectionStore:
    def __init__(self, store, *, now, new_id) -> None:
        self._s = store
        self._now = now
        self._new_id = new_id

    async def create(self, *, tenant: str, name: str, url: str, shape: str = "chat", model: str = "",
                     auth: str = "api_key", system_prompt: str = "", temperature: float = 0.2,
                     input_price_micros: int = 0, output_price_micros: int = 0,
                     currency: str = "SAR") -> AiConnection:
        if not tenant or not name or not url:
            raise ConnectionError("tenant, name and url are required")
        if shape not in _SHAPES:
            raise ConnectionError(f"shape must be one of {_SHAPES}")
        if auth not in _AUTHS:
            raise ConnectionError(f"auth must be one of {_AUTHS}")
        if int(input_price_micros) < 0 or int(output_price_micros) < 0:
            raise ConnectionError("prices must be >= 0")
        now = self._now()
        c = AiConnection(id=self._new_id(), tenant=tenant, name=name, url=url, shape=shape,
                         model=model, auth=auth, system_prompt=system_prompt,
                         temperature=float(temperature), input_price_micros=int(input_price_micros),
                         output_price_micros=int(output_price_micros), currency=currency,
                         created=now, updated=now)
        await self._s.put(_CONNECTIONS, _key(tenant, c.id), c.to_dict())
        return c

    async def get(self, tenant: str, cid: str) -> AiConnection | None:
        row = await self._s.get(_CONNECTIONS, _key(tenant, cid))
        return AiConnection.from_dict(row) if row else None

    async def list(self, tenant: str, *, limit: int = 100) -> list[dict[str, Any]]:
        if not tenant:
            raise ConnectionError("a tenant is required to list connections")
        rows, _ = await self._s.find(_CONNECTIONS, {"tenant": tenant}, limit=limit,
                                     sort=("created", "desc"))
        return rows

    async def delete(self, tenant: str, cid: str) -> bool:
        if await self.get(tenant, cid) is None:
            return False
        await self._s.delete(_CONNECTIONS, _key(tenant, cid))
        await self._s.delete(_USAGE, _key(tenant, cid))
        return True

    # ── usage ledger (per connection) ────────────────────────────────────────────
    async def record_usage(self, *, tenant: str, cid: str, tokens_in: int, tokens_out: int,
                           cost: int) -> None:
        row = await self._s.get(_USAGE, _key(tenant, cid)) or {}
        row.update(
            tenant=tenant, connection_id=cid,
            calls=int(row.get("calls", 0)) + 1,
            tokens_in=int(row.get("tokens_in", 0)) + int(tokens_in),
            tokens_out=int(row.get("tokens_out", 0)) + int(tokens_out),
            cost_micros=int(row.get("cost_micros", 0)) + int(cost),
            updated=self._now())
        await self._s.put(_USAGE, _key(tenant, cid), row)

    async def usage(self, tenant: str, cid: str) -> dict[str, Any]:
        row = await self._s.get(_USAGE, _key(tenant, cid))
        return row or {"tenant": tenant, "connection_id": cid, "calls": 0,
                       "tokens_in": 0, "tokens_out": 0, "cost_micros": 0}


async def invoke(conn: AiConnection, *, prompt: str, caller: Caller, store: ConnectionStore,
                 max_tokens: int = 0) -> dict[str, Any]:
    """Run ONE connection: its own prompt + the caller's message, meter its usage, return the answer
    and the cost. `caller` is a model bound to THIS connection's endpoint/credential — isolation is
    that the route builds a caller per connection and never shares it."""
    text, tin, tout = await caller(conn.system_prompt, prompt, max_tokens)
    cost = cost_micros(tin, tout, conn.input_price_micros, conn.output_price_micros)
    await store.record_usage(tenant=conn.tenant, cid=conn.id, tokens_in=tin, tokens_out=tout, cost=cost)
    return {"text": text, "tokens_in": tin, "tokens_out": tout,
            "cost_micros": cost, "currency": conn.currency, "connection_id": conn.id}
