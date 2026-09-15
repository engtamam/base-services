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
_SHAPES = ("chat", "generate", "anthropic")
_AUTHS = ("api_key", "vertex")


class ConnectionError(ValueError):
    """A connection definition the service refuses (bad shape/auth/name/price)."""


# caller(system, user, max_tokens) -> (text, tokens_in, tokens_out). Injected so core stays adapter-free.
Caller = Callable[[str, str, int], Awaitable[tuple[str, int, int]]]
# converse(system, messages, tools, max_tokens) -> a discriminated dict (text | tool_call) + tokens.
Converser = Callable[..., Awaitable[dict]]


def _validate_tools(tools: Any) -> list[dict]:
    """A connection's optional tool declarations. Each is {name, description?, parameters?} where
    parameters is a JSON-Schema object. Empty/absent by default so an untouched connection behaves
    exactly as before. Validated at the seam so a malformed declaration is a clean 422, not a
    provider error mid-conversation."""
    if not tools:
        return []
    if not isinstance(tools, list):
        raise ConnectionError("tools must be a list of {name, description, parameters}")
    out: list[dict] = []
    seen: set[str] = set()
    for t in tools:
        if not isinstance(t, dict) or not str(t.get("name", "")).strip():
            raise ConnectionError("each tool needs a non-empty name")
        name = str(t["name"]).strip()
        if name in seen:
            raise ConnectionError(f"duplicate tool name: {name}")
        seen.add(name)
        params = t.get("parameters") or {"type": "object", "properties": {}}
        if not isinstance(params, dict):
            raise ConnectionError(f"tool {name}: parameters must be a JSON-Schema object")
        out.append({"name": name, "description": str(t.get("description", "")), "parameters": params})
    return out


@dataclass
class AiConnection:
    id: str
    tenant: str
    name: str
    url: str
    shape: str = "chat"                  # chat | generate | anthropic
    model: str = ""
    auth: str = "api_key"                # api_key | vertex
    system_prompt: str = ""
    temperature: float = 0.2
    input_price_micros: int = 0          # micros per 1000 input tokens
    output_price_micros: int = 0         # micros per 1000 output tokens
    currency: str = "SAR"
    # Optional tool/function declarations for THIS connection only — opt-in, empty by default so an
    # untouched connection is text-only exactly as before. Never shared with another connection.
    tools: list = field(default_factory=list)
    created: str = ""
    updated: str = ""

    _FIELDS = ("id", "tenant", "name", "url", "shape", "model", "auth", "system_prompt",
               "temperature", "input_price_micros", "output_price_micros", "currency",
               "tools", "created", "updated")

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self._FIELDS}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AiConnection":
        return cls(**{k: d.get(k, getattr(cls, k, None)) for k in cls._FIELDS})

    def public(self) -> dict[str, Any]:
        # No secret is ever on the row (the key lives in the secret store), so the whole row is safe.
        return self.to_dict()


def cost_micros(tokens_in: int, tokens_out: int, in_price: int, out_price: int) -> int:
    """Integer micros. Rounded per side so a fractional 1k-token block still bills, never a float."""
    ci = (int(tokens_in) * int(in_price) + 999) // 1000 if tokens_in and in_price else 0
    co = (int(tokens_out) * int(out_price) + 999) // 1000 if tokens_out and out_price else 0
    return ci + co


def _legacy_key(tenant: str, cid: str) -> str:
    """The OLD composite store key. Kept only so the one-time re-key migration can find and remove
    legacy rows. Live reads/writes now key by the bare cid (see below)."""
    return f"{tenant}|{cid}"


class ConnectionStore:
    """Connections and their usage ledger are keyed by the BARE connection id (cid) — a globally
    unique uuid — so the datastore key equals the row's own `id`. That is what lets a backup restore
    each row under its own key (the key==id invariant); the previous composite key (tenant|cid) has
    no `id`-equal form and would have collapsed every restored row under one key. Tenant isolation,
    which the composite key used to give implicitly, is now an EXPLICIT guard on every read: a row
    whose tenant does not match is treated as absent (no cross-tenant read, an IDOR guard)."""

    def __init__(self, store, *, now, new_id) -> None:
        self._s = store
        self._now = now
        self._new_id = new_id

    async def create(self, *, tenant: str, name: str, url: str, shape: str = "chat", model: str = "",
                     auth: str = "api_key", system_prompt: str = "", temperature: float = 0.2,
                     input_price_micros: int = 0, output_price_micros: int = 0,
                     currency: str = "SAR", tools: Any = None) -> AiConnection:
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
                         tools=_validate_tools(tools), created=now, updated=now)
        await self._s.put(_CONNECTIONS, c.id, c.to_dict())
        return c

    _MUTABLE = ("name", "url", "shape", "model", "auth", "system_prompt", "temperature",
                "input_price_micros", "output_price_micros", "currency", "tools")

    async def update(self, tenant: str, cid: str, changes: dict[str, Any]) -> AiConnection | None:
        """Patch a connection in place (tools included). Only mutable fields change; id/tenant/created
        never move, and the credential is untouched (it lives in the secret store). Same validation as
        create, so an update can never leave a row create would have refused."""
        c = await self.get(tenant, cid)
        if c is None:
            return None
        for k, v in changes.items():
            if k not in self._MUTABLE or v is None:
                continue
            if k == "shape" and v not in _SHAPES:
                raise ConnectionError(f"shape must be one of {_SHAPES}")
            if k == "auth" and v not in _AUTHS:
                raise ConnectionError(f"auth must be one of {_AUTHS}")
            if k in ("input_price_micros", "output_price_micros"):
                if int(v) < 0:
                    raise ConnectionError("prices must be >= 0")
                v = int(v)
            if k == "temperature":
                v = float(v)
            if k == "tools":
                v = _validate_tools(v)
            setattr(c, k, v)
        c.updated = self._now()
        await self._s.put(_CONNECTIONS, c.id, c.to_dict())
        return c

    async def get(self, tenant: str, cid: str) -> AiConnection | None:
        row = await self._s.get(_CONNECTIONS, cid)
        # IDOR guard: a row keyed by the (global) cid must belong to THIS tenant, or it
        # reads as absent — the tenant scoping the composite key used to give implicitly.
        if not row or row.get("tenant") != tenant:
            return None
        return AiConnection.from_dict(row)

    async def list(self, tenant: str, *, limit: int = 100) -> list[dict[str, Any]]:
        if not tenant:
            raise ConnectionError("a tenant is required to list connections")
        rows, _ = await self._s.find(_CONNECTIONS, {"tenant": tenant}, limit=limit,
                                     sort=("created", "desc"))
        return rows

    async def delete(self, tenant: str, cid: str) -> bool:
        if await self.get(tenant, cid) is None:
            return False
        await self._s.delete(_CONNECTIONS, cid)
        await self._s.delete(_USAGE, cid)
        return True

    # ── usage ledger (per connection) ────────────────────────────────────────────
    async def record_usage(self, *, tenant: str, cid: str, tokens_in: int, tokens_out: int,
                           cost: int) -> None:
        # ATOMIC additive metering. A get()+put() pair here loses increments under concurrency: two
        # in-flight calls on the same connection both read calls=N and both write N+1, so the ledger
        # under-counts and the cost is UNDER-BILLED — a silent money leak that grows with traffic.
        # add_fields does the read-modify-write indivisibly, so every call's tokens and cost land.
        await self._s.add_fields(
            _USAGE, cid,
            {"calls": 1, "tokens_in": int(tokens_in), "tokens_out": int(tokens_out),
             "cost_micros": int(cost)},
            # id == cid so the usage row restores under its own key (key==id); tenant is stamped for
            # the IDOR guard on read.
            {"id": cid, "tenant": tenant, "connection_id": cid, "updated": self._now()})

    async def usage(self, tenant: str, cid: str) -> dict[str, Any]:
        row = await self._s.get(_USAGE, cid)
        # IDOR guard: another tenant's usage for the same (global) cid reads as empty, never leaked.
        if not row or row.get("tenant") != tenant:
            return {"tenant": tenant, "connection_id": cid, "calls": 0,
                    "tokens_in": 0, "tokens_out": 0, "cost_micros": 0}
        return row

    # ── one-time re-key migration (composite key -> bare cid) ────────────────────
    async def migrate_keys(self, *, cap: int = 100_000) -> dict[str, int]:
        """Re-key any legacy rows written under the old composite key (tenant|cid) to the bare cid, so
        the datastore key equals row['id'] and a backup can restore them. IDEMPOTENT: a row already at
        its bare-cid key is left alone; a legacy duplicate at the composite key is moved then deleted.
        Safe to run repeatedly (an operator calls it once after upgrade). Bounded by `cap`.

        Reads values only (scan_all returns no keys), but a row carries everything needed to recompute
        its OWN old key: connections carry id + tenant; usage rows carry connection_id + tenant. A row
        missing that identity is skipped (counted) rather than guessed at."""
        moved = skipped = 0
        for coll, id_field in ((_CONNECTIONS, "id"), (_USAGE, "connection_id")):
            # sweeper: a ONE-TIME operator-run migration over the whole (small) connection/usage
            # catalogue, not a request path — re-keying every row IS the operation, and it is bounded
            # by `cap`. Reads values only (scan_all returns no keys); a find() cannot express "every row".
            for row in await self._s.scan_all(coll, cap):
                cid = str(row.get(id_field) or "")
                tenant = str(row.get("tenant") or "")
                if not cid or not tenant:
                    skipped += 1
                    continue
                legacy = _legacy_key(tenant, cid)
                if legacy == cid:                       # nothing to move (defensive)
                    continue
                if await self._s.get(coll, legacy) is None:
                    continue                            # already migrated (or never legacy)
                row = {**row, "id": cid}                # ensure key==id on the moved row (usage lacked it)
                await self._s.put(coll, cid, row)       # write under the bare key
                await self._s.delete(coll, legacy)      # remove the composite duplicate
                moved += 1
        return {"moved": moved, "skipped": skipped}


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


def _normalize_messages(messages: Any) -> list[dict]:
    """Accept a plain string (today's single-shot shape) OR a list of transcript turns. A string
    becomes one user turn, so a tool-less caller need not change. A list is passed through as the
    running conversation."""
    if isinstance(messages, str):
        return [{"role": "user", "content": messages}]
    if isinstance(messages, list):
        return messages
    raise ConnectionError("messages must be a string or a list of {role, content} turns")


async def invoke_conversation(conn: AiConnection, *, messages: Any, converse: Converser,
                              store: ConnectionStore, max_tokens: int = 0) -> dict[str, Any]:
    """Run ONE round trip of a (possibly tool-calling) conversation on THIS connection, using its own
    system prompt and its own declared tools, and meter this round trip's usage. Returns a
    discriminated result: {"kind":"text","text":...} or {"kind":"tool_call","calls":[...]}, plus the
    per-round-trip cost. The caller loops: on a tool_call it executes the tool and calls again with
    the result appended — each call meters itself, so a multi-round turn accumulates in the ledger.

    Isolation is unchanged: `converse` is bound to this connection's endpoint/credential, and only
    this connection's tools are ever sent — never another connection's, another tenant's, or the
    service's built-in KB assistant."""
    turns = _normalize_messages(messages)
    result = await converse(system=conn.system_prompt, messages=turns, tools=list(conn.tools or []),
                            max_tokens=max_tokens)
    tin, tout = int(result.get("tokens_in", 0)), int(result.get("tokens_out", 0))
    cost = cost_micros(tin, tout, conn.input_price_micros, conn.output_price_micros)
    await store.record_usage(tenant=conn.tenant, cid=conn.id, tokens_in=tin, tokens_out=tout, cost=cost)
    out = {"kind": result.get("kind", "text"), "tokens_in": tin, "tokens_out": tout,
           "cost_micros": cost, "currency": conn.currency, "connection_id": conn.id}
    if result.get("kind") == "tool_call":
        out["calls"] = result.get("calls", [])
    else:
        out["text"] = result.get("text", "")
    return out
