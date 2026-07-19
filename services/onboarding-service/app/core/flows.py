"""Load and validate flow declarations.

Every check here catches something that would otherwise strand a real person mid-onboarding — the
worst place to find a typo, because the customer cannot proceed and cannot see why.

  a cycle in `blocks`             nothing is ever attemptable; the flow can never complete
  a dependency on a missing id    a requirement waits forever on something that does not exist
  a duplicate id                  two definitions, and which applies depends on list order
  an unknown `satisfied_by`       nothing can ever satisfy it
  a grant with no `once_per`      the gift can be farmed by signing up again

That last one is a WARNING shape rather than an error: a platform may legitimately grant something
on every flow (a welcome email, a tutorial flag). But money-like grants without a once_per key are
how both reference implementations we studied could be farmed, so the validator names it.
"""
from __future__ import annotations

from typing import Any

# The sources a requirement may be satisfied by. `local.*` means this service owns the fact —
# everything else names the service that does, and we ASK it.
KNOWN_SOURCES = ("identity", "tenant", "verification", "billing", "wallet", "file", "local")
SCOPES = ("user", "tenant")


class FlowError(ValueError):
    """A malformed flow. Raised at boot, never swallowed."""


def validate(flows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen_flows: set[str] = set()
    for flow in flows:
        name = flow.get("flow", "")
        if not name or not isinstance(name, str):
            raise FlowError("every flow needs a non-empty 'flow' name")
        if name in seen_flows:
            raise FlowError(f"flow {name!r} is declared twice")
        seen_flows.add(name)

        requirements = flow.get("requirements") or []
        if not requirements:
            raise FlowError(f"flow {name!r} has no requirements — it would complete instantly")

        ids: set[str] = set()
        for req in requirements:
            rid = req.get("id", "")
            if not rid or not isinstance(rid, str):
                raise FlowError(f"{name}: every requirement needs an 'id'")
            if rid in ids:
                raise FlowError(
                    f"{name}: requirement {rid!r} is declared twice — which one applies would "
                    f"depend on list order")
            ids.add(rid)

            source = req.get("satisfied_by", "")
            if not source or "." not in source:
                raise FlowError(
                    f"{name}:{rid} needs satisfied_by as 'service.fact', e.g. "
                    f"'identity.email_verified' — it names WHO OWNS the truth")
            service = source.split(".", 1)[0]
            if service not in KNOWN_SOURCES:
                raise FlowError(
                    f"{name}:{rid} is satisfied_by {source!r}, but {service!r} is not a known "
                    f"source ({', '.join(KNOWN_SOURCES)}) — nothing could ever satisfy it")

            scope = req.get("scope", "user")
            if scope not in SCOPES:
                raise FlowError(f"{name}:{rid} has scope {scope!r}; must be one of {SCOPES}")

            if req.get("grants") and not req.get("once_per"):
                # Not fatal — a tutorial flag may legitimately be granted every time. But a
                # money-like grant without a uniqueness key is farmable by signing up again, which
                # is exactly how the implementations we studied could be drained.
                req.setdefault("_warning", "grants without once_per can be issued repeatedly")

        # Dependencies must exist, and must not form a cycle.
        for req in requirements:
            for blocked in req.get("blocks") or []:
                if blocked not in ids:
                    raise FlowError(
                        f"{name}:{req['id']} blocks {blocked!r}, which is not a requirement of "
                        f"this flow — the dependency would never resolve")
        _refuse_cycles(name, requirements)

    return flows


def _refuse_cycles(flow_name: str, requirements: list[dict]) -> None:
    """A cycle means nothing is ever attemptable and the flow can never complete.

    Found by depth-first walk rather than by counting, so the error can NAME the cycle — telling an
    author "there is a cycle somewhere in nine requirements" is barely better than silence.
    """
    edges: dict[str, list[str]] = {r["id"]: list(r.get("blocks") or []) for r in requirements}
    WHITE, GREY, BLACK = 0, 1, 2
    colour = dict.fromkeys(edges, WHITE)

    def walk(node: str, path: list[str]) -> None:
        colour[node] = GREY
        for nxt in edges.get(node, []):
            if colour.get(nxt) == GREY:
                cycle = " -> ".join(path[path.index(nxt):] + [nxt])
                raise FlowError(
                    f"{flow_name}: requirements form a cycle ({cycle}); nothing in it could ever "
                    f"be attempted, so the flow could never complete")
            if colour.get(nxt) == WHITE:
                walk(nxt, path + [nxt])
        colour[node] = BLACK

    for node in edges:
        if colour[node] == WHITE:
            walk(node, [node])


def pick_flow(flows: list[dict[str, Any]], profile: dict[str, Any]) -> dict[str, Any] | None:
    """The first flow whose audience matches. First match wins, so specific flows come first.

    Returns None rather than a default when nothing matches: guessing a flow would put someone
    through the wrong requirements — asking a personal account for a commercial registration, or
    worse, letting a business skip verification and collect a gift.
    """
    for flow in flows:
        audience = flow.get("audience") or {}
        if all(profile.get(k) == v for k, v in audience.items()):
            return flow
    return None
