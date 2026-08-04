"""Ticket types and their workflows — a state machine expressed as DATA, not code.

This is the load-bearing idea of the whole service. A ticket TYPE (feedback, support, billing…) is
not a code path; it is a config object carrying its own statuses, the allowed transitions between
them, which capability may make each move, which fields are required to open one, and what to
notify. Adding a type later is a config entry — a setting, not a project.

WHY A STATE MACHINE AND NOT A STATUS LIST. A flat list of statuses lets any status follow any other:
`closed → new`, `new → resolved` skipping the work. A workflow is the transitions, and enforcing
them server-side is what makes "new → in_progress → resolved → closed" a rule rather than a
suggestion the client is trusted to keep.

FAIL AT BOOT, NOT AT THE FIRST TICKET. An incoherent type — a transition to a status that does not
exist, no terminal state, a status nothing can reach — is refused when the registry is built, so a
malformed workflow is a failed deploy, not a ticket that can never be closed. Same discipline as the
rest of the platform: inert is not honest.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class WorkflowError(ValueError):
    """A ticket-type definition that is incoherent. Raised at boot, never swallowed."""


@dataclass(frozen=True)
class TicketType:
    key: str
    label: dict[str, str]                       # {"en": "...", "ar": "..."}
    statuses: tuple[str, ...]
    start: str                                  # the status a freshly opened ticket holds
    transitions: dict[str, tuple[str, ...]]     # from -> allowed to
    terminal: tuple[str, ...]                   # statuses that accept no further transition
    required_fields: tuple[str, ...] = ()       # fields that must be present to open this type
    reply_capability: str = "tickets.self"      # who may add a reply
    transition_capabilities: dict[str, str] = field(default_factory=dict)  # target-status -> cap
    notify: dict[str, Any] = field(default_factory=dict)                   # {on:[...], template_prefix}

    def can_transition(self, frm: str, to: str) -> bool:
        return to in self.transitions.get(frm, ())

    def is_terminal(self, status: str) -> bool:
        return status in self.terminal

    def capability_for(self, to_status: str, default: str) -> str:
        """The capability required to move a ticket INTO `to_status`. Defaults to the operator cap
        so an undeclared transition is never accidentally self-serviceable."""
        return self.transition_capabilities.get(to_status, default)

    def public(self) -> dict[str, Any]:
        """What the app shows to render a workflow — never trusted as authority, the API re-checks."""
        return {"key": self.key, "label": dict(self.label), "statuses": list(self.statuses),
                "start": self.start, "transitions": {k: list(v) for k, v in self.transitions.items()},
                "terminal": list(self.terminal), "required_fields": list(self.required_fields)}


class WorkflowRegistry:
    """The parsed, validated set of ticket types. Built once at boot from config."""

    def __init__(self, types: dict[str, TicketType]) -> None:
        if not types:
            raise WorkflowError("no ticket types configured — at least one is required")
        self._types = types

    def get(self, key: str) -> TicketType | None:
        return self._types.get(key)

    def require(self, key: str) -> TicketType:
        t = self._types.get(key)
        if t is None:
            raise WorkflowError(f"unknown ticket type {key!r}")
        return t

    def keys(self) -> list[str]:
        return sorted(self._types)

    def public(self) -> list[dict[str, Any]]:
        return [self._types[k].public() for k in self.keys()]

    # ── construction + coherence ────────────────────────────────────────────────
    @classmethod
    def from_config(cls, raw: list[dict[str, Any]] | None,
                    *, default_transition_cap: str = "tickets.manage") -> "WorkflowRegistry":
        """Parse and VALIDATE the type config. Every incoherence is a boot-time WorkflowError."""
        if not raw:
            raise WorkflowError("TICKET_TYPES is empty — configure at least one ticket type")
        types: dict[str, TicketType] = {}
        for i, entry in enumerate(raw):
            if not isinstance(entry, dict):
                raise WorkflowError(f"ticket type #{i} is not an object")
            key = str(entry.get("key", "")).strip()
            if not key:
                raise WorkflowError(f"ticket type #{i} has no key")
            if key in types:
                raise WorkflowError(f"duplicate ticket type key {key!r}")
            statuses = tuple(str(s) for s in (entry.get("statuses") or []))
            if not statuses:
                raise WorkflowError(f"type {key!r}: statuses must be a non-empty list")
            if len(set(statuses)) != len(statuses):
                raise WorkflowError(f"type {key!r}: duplicate status")
            start = str(entry.get("start", statuses[0]))
            if start not in statuses:
                raise WorkflowError(f"type {key!r}: start {start!r} is not one of its statuses")
            raw_trans = entry.get("transitions") or {}
            transitions: dict[str, tuple[str, ...]] = {}
            for frm, tos in raw_trans.items():
                if frm not in statuses:
                    raise WorkflowError(f"type {key!r}: transition from unknown status {frm!r}")
                tos_t = tuple(str(t) for t in (tos or []))
                for t in tos_t:
                    if t not in statuses:
                        raise WorkflowError(
                            f"type {key!r}: transition {frm!r} -> {t!r} names unknown status {t!r}")
                transitions[frm] = tos_t
            terminal = tuple(str(s) for s in (entry.get("terminal") or []))
            for s in terminal:
                if s not in statuses:
                    raise WorkflowError(f"type {key!r}: terminal status {s!r} is not a status")
                if transitions.get(s):
                    raise WorkflowError(
                        f"type {key!r}: terminal status {s!r} must have no outgoing transitions")
            if not terminal:
                raise WorkflowError(
                    f"type {key!r}: at least one terminal status is required, or a ticket can "
                    f"never be closed")
            # Reachability: every status except the start must be reachable from the start, or it is
            # a dead status no ticket can ever hold — a workflow bug that looks like a feature.
            reachable = {start}
            frontier = [start]
            while frontier:
                cur = frontier.pop()
                for nxt in transitions.get(cur, ()):
                    if nxt not in reachable:
                        reachable.add(nxt)
                        frontier.append(nxt)
            dead = set(statuses) - reachable
            if dead:
                raise WorkflowError(
                    f"type {key!r}: status(es) {sorted(dead)} are unreachable from start {start!r}")
            tcaps = {str(k): str(v) for k, v in (entry.get("transition_capabilities") or {}).items()}
            for s in tcaps:
                if s not in statuses:
                    raise WorkflowError(
                        f"type {key!r}: transition_capabilities names unknown status {s!r}")
            types[key] = TicketType(
                key=key,
                label={str(k): str(v) for k, v in (entry.get("label") or {}).items()} or {"en": key},
                statuses=statuses, start=start, transitions=transitions, terminal=terminal,
                required_fields=tuple(str(f) for f in (entry.get("required_fields") or [])),
                reply_capability=str(entry.get("reply_capability", "tickets.self")),
                transition_capabilities=tcaps,
                notify=dict(entry.get("notify") or {}))
        return cls(types)
