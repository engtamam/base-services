#!/usr/bin/env python3
"""safety:audit-wiring — a service that can act must be able to prove it did.

This is the second instance of a defect that has now bitten this repo twice: code that is complete,
correct and connected to nothing. Ten erasure endpoints had no callers (Phase 37). audit-service
shipped a hash-chained log with a working ingest endpoint that, for months, no service called — the
chain verified perfectly and held zero events, and not one test went red, because a missing event
leaves no hole. An audit log cannot tell you what it never received.

No unit test can see this. Each half is individually correct: the declaration is valid, the sink
works, the config parses. Only a check that reads the DECLARATIONS and the DEPLOYED CONFIG together
can see the seam between them, which is what this gate is.

  R1  A service declares auditable actions but is not configured to emit.
      It knows what it should record and has nowhere to send it. Every action proceeds unwitnessed
      while the code looks complete.

  R2  A service is configured to emit but declares nothing.
      A sink pointed at the log with no actions declared records nothing at all — the config
      creates the appearance of an audit trail and produces an empty one, which is exactly the
      state this phase found the platform in.

  R3  A service declares actions but NEVER EMITS any.
      The declarations are valid, the sink is wired, the config points at the log — and no code
      path calls record(), so the chain stays empty. This gate SHIPPED with exactly that hole:
      every check passed while nothing was recorded, which is the same defect one level deeper.
      Declaring an action is a promise; emitting it is the only thing that keeps it.

  R4  A declaration is malformed.
      Checked with ast.literal_eval so the gate never imports (and therefore executes) a service.
      A critical action with no `why` is refused here as well as at boot, because `critical` means
      "refuse live requests when the log is down" and that decision must be deliberate.

Exit 1 on any violation, naming the service and which side of the seam is missing.
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICES = ROOT / "services"
VALUES = ROOT / "deploy" / "services"
SOURCES = frozenset({"admin", "user", "internal", "auth", "system"})

# Not a deployed service: it ships the contract so a new service is born wired.
EXEMPT = {"_template": "the scaffold — it carries the declaration format for new services"}


def declarations(service: pathlib.Path) -> list[dict] | None:
    """The service's AUDITABLE_ACTIONS, or None if it declares none.

    Read as a pure literal: the gate must never import a service to inspect it, because importing
    runs module-level code and a gate that executes what it audits is not a gate.
    """
    decl = service / "app" / "auditable_actions.py"
    if not decl.exists():
        return None
    tree = ast.parse(decl.read_text(encoding="utf-8"))
    for node in tree.body:
        target = getattr(node, "target", None) or (getattr(node, "targets", [None]) or [None])[0]
        if getattr(target, "id", "") == "AUDITABLE_ACTIONS":
            try:
                return ast.literal_eval(node.value)
            except (ValueError, SyntaxError) as exc:
                raise ValueError(
                    f"AUDITABLE_ACTIONS is not a pure literal ({exc}). No variables, f-strings or "
                    f"function calls — the gate reads it without importing your service.") from exc
    return None


def emitted_actions(service: pathlib.Path) -> set[str]:
    """Action names this service actually passes to auditor.record().

    Read as source text, not by importing: the gate must never execute what it audits. It matches
    the literal first argument, which is the only form that can be checked statically — and is the
    form every call should use anyway, because a computed action name cannot be found by anyone
    searching the log either.
    """
    found: set[str] = set()
    for path in (service / "app").rglob("*.py"):
        if "__pycache__" in str(path):
            continue
        # One or more dots, not exactly one. validate() requires only that an action CONTAINS a
        # dot, so `catalog.draft.approved` is a legal declaration — and the earlier two-segment
        # pattern could not see its emit call, reporting a correctly-wired action as missing.
        #
        # Note which way that failed: it cried wolf rather than staying silent, because an
        # unmatched emit reads as "never emitted". Noise, not a hole — a genuinely missing
        # three-part action is still caught. But it sends someone hunting for a call that is
        # already there, and a gate people learn to disbelieve stops working long before it
        # stops running.
        for match in re.finditer(r"""record\(\s*["']([a-z0-9_]+(?:\.[a-z0-9_]+)+)["']""",
                                 path.read_text(encoding="utf-8")):
            found.add(match.group(1))
    return found


def emits(name: str) -> bool:
    """Is this service deployed with a sink that actually reaches the log?"""
    values = VALUES / f"{name}.values.yaml"
    if not values.exists():
        return False
    text = values.read_text(encoding="utf-8")
    match = re.search(r'^\s*AUDIT_SINK_DRIVER:\s*"?([a-z]+)"?', text, re.MULTILINE)
    return bool(match) and match.group(1) != "null"


def malformed(actions: list[dict]) -> list[str]:
    """The same rules the service enforces at boot, applied before it ever runs."""
    problems, seen = [], set()
    for entry in actions:
        action = entry.get("action", "")
        if not isinstance(action, str) or "." not in action:
            problems.append(f"action {action!r} must be a dotted noun.verb, e.g. 'user.suspended'")
            continue
        if action in seen:
            problems.append(f"action {action!r} is declared twice — two criticality answers, and "
                            f"which one applies would depend on list order")
        seen.add(action)
        if entry.get("source") not in SOURCES:
            problems.append(f"action {action!r} has source {entry.get('source')!r}; must be one of "
                            f"{', '.join(sorted(SOURCES))}")
        if not isinstance(entry.get("critical"), bool):
            problems.append(f"action {action!r} must set critical to True or False explicitly")
        elif entry["critical"] and not str(entry.get("why") or "").strip():
            problems.append(f"action {action!r} is critical (it REFUSES live requests when the "
                            f"audit log is down) but states no 'why'")
    return problems


def main() -> int:
    problems: list[str] = []
    wired = 0

    for service in sorted(p for p in SERVICES.iterdir() if p.is_dir()):
        name = service.name
        if not (service / "app").exists():
            continue
        try:
            actions = declarations(service)
        except ValueError as exc:
            problems.append(f"  {name}: {exc}")
            continue

        if name in EXEMPT:
            if actions:
                problems.extend(f"  {name}: {p}" for p in malformed(actions))
            continue

        emitting = emits(name)

        if actions and not emitting:
            problems.append(
                f"  {name}: declares {len(actions)} auditable action(s) but AUDIT_SINK_DRIVER is\n"
                f"      unset or 'null' — it knows what to record and has nowhere to send it.\n"
                f"      Fix: set AUDIT_SINK_DRIVER (and AUDIT_SERVICE_URL) in\n"
                f"      scripts/gen_deploy_values.py, then regenerate.")
        elif emitting and not actions:
            problems.append(
                f"  {name}: is configured to emit but declares NO auditable actions — the config\n"
                f"      creates the appearance of an audit trail and produces an empty one.\n"
                f"      Fix: copy services/_template/app/auditable_actions.py and fill it in.")
        elif not actions and not emitting:
            problems.append(
                f"  {name}: has no audit trail at all. Every privileged action it performs is\n"
                f"      unprovable. Fix: copy services/_template/app/auditable_actions.py.")
        else:
            wired += 1
            problems.extend(f"  {name}: {p}" for p in malformed(actions))

            # R3. Declaring is a promise; emitting is what keeps it. Enforced on CRITICAL actions
            # only, and that limit is deliberate: a critical action that is never emitted is a
            # security control that exists solely on paper, while a routine one missing an emit
            # costs visibility rather than evidence. Widening this to every action would be
            # stricter and would also make the gate reject work in progress on its first commit.
            emitted = emitted_actions(service)
            missing = sorted(a["action"] for a in actions
                             if a.get("critical") and a.get("action") not in emitted)
            if missing:
                problems.append(
                    f"  {name}: declares {len(missing)} CRITICAL action(s) that nothing ever\n"
                    f"      emits: {', '.join(missing)}\n"
                    f"      The declaration, the sink and the config are all in place, so every\n"
                    f"      check passes while the chain stays empty — the same defect one level\n"
                    f"      deeper. Fix: call `await request.app.state.auditor.record(\"<action>\",\n"
                    f"      ...)` at the point the action happens.")

    if problems:
        print("      audit trail is not wired to reality:")
        print("\n".join(problems))
        return 1

    print(f"      {wired} services declare auditable actions and are configured to record them")
    return 0


if __name__ == "__main__":
    sys.exit(main())
