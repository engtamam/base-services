"""Evaluate a flow: what is satisfied, what is blocked, what is left.

This is the whole service in one function. There is no stored cursor, no "current step", no
progress counter — a person's state is derived from asking the owning services what is true right
now. Everything else falls out of that:

  **Resume is free.** Someone who abandoned three weeks ago is re-evaluated on their next request.
  **Reordering is free.** `blocks` is a dependency graph; two unblocked requirements can be done in
  either order or simultaneously.
  **Invited users are free.** Their requirements are already satisfied. Nothing is skipped, because
  there was never a sequence to skip within.
  **Regression is honest.** A verification that expires makes its requirement UNSATISFIED again,
  and anything it blocked becomes blocked again. A stored tick could never do that.

The one rule that keeps it safe: **`unknown` never counts as satisfied.** A requirement we could
not evaluate leaves the flow incomplete and says which source we could not reach. An onboarding
service that treated an unreachable identity-service as "probably fine" would hand out whatever
completion grants, precisely while the platform was unhealthy.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from app.ports.requirement_source import SATISFIED, UNKNOWN, Answer

LOCAL = "local"


@dataclass
class RequirementState:
    id: str
    status: str                       # satisfied | unsatisfied | unknown
    optional: bool = False
    scope: str = "user"
    blocked_by: list[str] = field(default_factory=list)
    grants: str = ""
    detail: str = ""

    @property
    def satisfied(self) -> bool:
        """THE definition, used by everything below.

        Written as `== SATISFIED`, never `!= UNSATISFIED`: the second form makes every status
        added later — starting with `unknown` — default to satisfied.
        """
        return self.status == SATISFIED

    @property
    def attemptable(self) -> bool:
        """Can the person act on this NOW? Not satisfied, and nothing is holding it up."""
        return not self.satisfied and not self.blocked_by

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "status": self.status, "optional": self.optional,
                "scope": self.scope, "blocked_by": self.blocked_by, "grants": self.grants,
                "detail": self.detail, "attemptable": self.attemptable}


@dataclass
class FlowState:
    flow: str
    requirements: list[RequirementState]

    @property
    def complete(self) -> bool:
        """Complete when every REQUIRED requirement is satisfied.

        Optional ones never block — that is what optional means, and conflating the two is how a
        nudge to upload an avatar ends up gating access to a paid product.

        Note this is derived, never stored. A flow that was complete yesterday can be incomplete
        today if a verification lapsed, and reporting it as still complete would be the same class
        of lie as a DSAR marked done with data still out there.
        """
        return all(r.satisfied for r in self.requirements if not r.optional)

    @property
    def next_up(self) -> list[RequirementState]:
        """What the person can actually do right now, required work first.

        Sorted so a UI rendering "next step" picks something that unblocks progress rather than an
        optional nicety.
        """
        actionable = [r for r in self.requirements if r.attemptable]
        return sorted(actionable, key=lambda r: (r.optional, r.id))

    def to_dict(self) -> dict[str, Any]:
        return {
            "flow": self.flow,
            "complete": self.complete,
            # Deliberately NOT a step number or a percentage. A count of what is left is honest
            # about a set; "step 3 of 7" implies an order that does not exist.
            "remaining": [r.id for r in self.requirements if not r.satisfied and not r.optional],
            "next": [r.to_dict() for r in self.next_up],
            "requirements": [r.to_dict() for r in self.requirements],
        }


class Evaluator:
    def __init__(self, *, sources: dict, local_facts=None) -> None:
        self._sources = sources
        # Facts THIS service owns — chiefly "did we already issue this grant". Everything else
        # belongs to another service and is asked for, never stored here.
        self._local = local_facts

    async def evaluate(self, flow: dict[str, Any], *, tenant: str, subject: str) -> FlowState:
        answers: dict[str, RequirementState] = {}
        reqs = flow.get("requirements") or []

        # onboarding-010: ask the independent sources CONCURRENTLY (latency = max, not sum), and DEDUPE
        # remote lookups by (source, fact) so one service is queried once per distinct fact even when
        # several requirements share it. Local facts are checked per-requirement (local.check reads req).
        remote: dict[tuple[str, str], Any] = {}
        for req in reqs:
            source_name, _, fact = req["satisfied_by"].partition(".")
            if source_name != LOCAL and (source_name, fact) not in remote:
                # NB: build the task only when the key is new — setdefault would eagerly create (and
                # schedule) a duplicate coroutine for a fact already queued, defeating the dedupe.
                remote[(source_name, fact)] = asyncio.ensure_future(
                    self._ask(source_name, fact, tenant, subject, req))

        async def _resolve(req):
            source_name, _, fact = req["satisfied_by"].partition(".")
            if source_name != LOCAL:
                return req["id"], await remote[(source_name, fact)]
            return req["id"], await self._ask(source_name, fact, tenant, subject, req)

        for rid, answer in await asyncio.gather(*[_resolve(r) for r in reqs]):
            req = next(r for r in reqs if r["id"] == rid)
            answers[rid] = RequirementState(
                id=rid,
                status=answer.status,
                optional=bool(req.get("optional")),
                scope=req.get("scope", "user"),
                grants=req.get("grants", ""),
                detail=answer.detail,
            )

        # Blocking is computed AFTER every answer is in, because a requirement is blocked by the
        # current state of its blocker — not by declaration order.
        for req in flow.get("requirements") or []:
            for blocked_id in req.get("blocks") or []:
                if blocked_id in answers and not answers[req["id"]].satisfied:
                    answers[blocked_id].blocked_by.append(req["id"])

        return FlowState(flow=flow["flow"], requirements=list(answers.values()))

    async def _ask(self, source_name: str, fact: str, tenant: str, subject: str,
                   req: dict) -> Answer:
        if source_name == LOCAL:
            if self._local is None:
                return Answer(status=UNKNOWN, detail="no local fact resolver")
            return await self._local.check(fact=fact, tenant=tenant, subject=subject, req=req)

        source = self._sources.get(source_name)
        if source is None:
            # Not configured. Unknown, never satisfied: a flow whose requirements are all unknown
            # never completes, which is visibly broken rather than silently permissive.
            return Answer(status=UNKNOWN, detail=f"no source configured for {source_name!r}")
        try:
            return await source.check(fact=fact, tenant=tenant, subject=subject)
        except Exception as exc:
            # The port forbids raising, but an adapter is ordinary code and a bug in it must not
            # become a satisfied requirement.
            return Answer(status=UNKNOWN, detail=f"{source_name} source raised: {exc}")
