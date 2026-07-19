"""Load and validate the auditable-action declarations.

Validation runs at BOOT rather than at the point of use, because both mistakes this catches are
silent otherwise:

  * A typo in an action name means the log fills with `user.suspeneded` and the query an
    investigator runs returns nothing. The events exist; they are simply unfindable.
  * `critical: True` with no `why` means someone marked an action as "refuse the request when the
    log is down" without stating what would be lost. That is an availability decision made by
    accident, and the `why` is what forces it to be made on purpose.

Neither shows up in a test of the happy path, and neither surfaces until an investigation needs the
record and it is not there.
"""
from __future__ import annotations

from typing import Any

SOURCES = frozenset({"admin", "user", "internal", "auth", "system"})


class DeclarationError(ValueError):
    """A malformed auditable-action declaration. Raised at boot, never swallowed."""


def validate(actions: list[dict[str, Any]], service: str) -> list[dict[str, Any]]:
    seen: set[str] = set()
    for entry in actions:
        action = entry.get("action", "")
        if not action or not isinstance(action, str):
            raise DeclarationError(f"{service}: every entry needs a non-empty 'action'")
        if "." not in action:
            # The dot is what lets an investigator query a whole family ("everything that happened
            # to permissions"). A flat name is only ever findable if you already know it.
            raise DeclarationError(
                f"{service}: action {action!r} must be dotted noun.verb, e.g. 'user.suspended'")
        if action in seen:
            # Two declarations for one action mean two different criticality answers, and which one
            # applies would depend on list order.
            raise DeclarationError(f"{service}: action {action!r} is declared twice")
        seen.add(action)

        source = entry.get("source")
        if source not in SOURCES:
            raise DeclarationError(
                f"{service}: action {action!r} has source {source!r}; must be one of "
                f"{', '.join(sorted(SOURCES))}")

        if not isinstance(entry.get("critical"), bool):
            # Absent or truthy-but-not-bool would make "is this refused when audit is down?"
            # depend on Python truthiness rather than on a decision someone made.
            raise DeclarationError(
                f"{service}: action {action!r} must set critical to True or False explicitly")

        if entry["critical"] and not (entry.get("why") or "").strip():
            raise DeclarationError(
                f"{service}: action {action!r} is critical (it will REFUSE the request when the "
                f"audit log is unavailable) but states no 'why'. Write the line an investigator "
                f"would lose without it — if you cannot, it is probably not critical.")
    return actions


def critical_actions(actions: list[dict[str, Any]]) -> frozenset[str]:
    return frozenset(a["action"] for a in actions if a.get("critical"))


def declared_actions(actions: list[dict[str, Any]]) -> frozenset[str]:
    return frozenset(a["action"] for a in actions)
