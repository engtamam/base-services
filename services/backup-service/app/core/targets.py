"""The services backup-service protects — registered, never hardcoded.

A target is any service that answers the backup contract: a base URL and the reference to the
internal token used to reach it. base-services' own services and a third party's are the same shape
here; nothing distinguishes them, which is the whole point of the contract being public.

Registration rather than a config list, because the set of services a platform runs is that
platform's decision and changes without a redeploy of the orchestrator — and because a third party
joins by registering, exactly as billing registers a wallet URL.

THE TOKEN IS NOT STORED HERE IN THE CLEAR. What is kept is a REFERENCE — the name of the operator
secret holding the token — resolved through the credential store at call time. A registry row is
therefore not a credential dump: a stolen targets table yields URLs and secret names, not the tokens
themselves.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_COLL = "backup_targets"


@dataclass
class Target:
    service: str                       # the target's own name, its id here
    base_url: str
    token_ref: str = ""                # the operator-secret name holding its internal token
    enabled: bool = True               # a target may be paused without being forgotten
    created: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "Target":
        known = {k: v for k, v in row.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def public(self) -> dict[str, Any]:
        """What an operator may see — never the token, only the reference to it."""
        return {"service": self.service, "base_url": self.base_url, "token_ref": self.token_ref,
                "enabled": self.enabled, "created": self.created, "note": self.note}


class TargetStore:
    def __init__(self, store) -> None:
        self._s = store

    async def upsert(self, target: Target) -> Target:
        await self._s.put(_COLL, target.service, target.to_dict())
        return target

    async def get(self, service: str) -> Target | None:
        row = await self._s.get(_COLL, service)
        return Target.from_dict(row) if row else None

    async def remove(self, service: str) -> bool:
        if await self._s.get(_COLL, service) is None:
            return False
        await self._s.delete(_COLL, service)
        return True

    async def all(self, limit: int) -> list[Target]:
        # sweeper: the target registry is the orchestrator's own small control table — one row per
        # service it protects, global, not per tenant — so reading it whole IS the operation.
        rows = await self._s.scan_all(_COLL, limit)
        return [Target.from_dict(r) for r in rows]

    async def enabled(self, limit: int) -> list[Target]:
        return [t for t in await self.all(limit) if t.enabled]
