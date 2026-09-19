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
import os
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


def unrecorded_actions(service: pathlib.Path) -> dict[str, str]:
    """The service's UNRECORDED_ACTIONS escape hatch — {action: why} for actions it DELIBERATELY
    does not record here (one another service records, or a not-yet-built path). Same literal-only
    read as declarations(). An entry without a non-empty `why` is ignored, so the reason is
    mandatory: an omission must be a decision someone wrote down, not an oversight with extra steps.
    """
    decl = service / "app" / "auditable_actions.py"
    if not decl.exists():
        return {}
    try:
        tree = ast.parse(decl.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return {}
    for node in tree.body:
        target = getattr(node, "target", None) or (getattr(node, "targets", [None]) or [None])[0]
        if getattr(target, "id", "") == "UNRECORDED_ACTIONS":
            try:
                rows = ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                return {}
            return {r["action"]: r["why"] for r in rows
                    if isinstance(r, dict) and isinstance(r.get("action"), str)
                    and str(r.get("why", "")).strip()}
    return {}


_ANON_ACTOR = re.compile(r"""record\(\s*["']([a-z0-9_.]+)["'][^)]*?actor\s*=\s*["']["']""", re.S)


def anonymous_emits(service: pathlib.Path) -> list[str]:
    """Actions recorded on a USER-FACING route with a literal empty actor.

    An audit entry with no actor answers what and when but not WHO, and `who` is the only question
    an audit log exists to answer in a dispute. It is worse than no entry, because the log looks
    complete: "this tenant was suspended" with nobody named reads as a working control right up to
    the moment someone has to rely on it.

    The cause is mechanical, which is why this can be a gate at all. A route written

        @router.post(..., dependencies=[Depends(_ADMIN)])
        async def act(request: Request, ...)

    has no principal in scope, so whoever wrote it passed "". Written

        async def act(request: Request, ..., principal: Principal = Depends(_ADMIN))

    the name is right there. BOTH SPELLINGS AUTHORIZE IDENTICALLY — only one preserves attribution,
    and nothing but this check makes the difference visible.

    app/api/internal.py is exempt BY DESIGN, not by oversight: service-to-service calls have no
    human actor, and inventing one there would be worse than an empty string. That is why this
    walks api/v1 rather than all of app/.
    """
    found: list[str] = []
    v1 = service / "app" / "api" / "v1"
    if not v1.is_dir():
        return found
    for path in sorted(v1.rglob("*.py")):
        if "__pycache__" in str(path):
            continue
        for action in _ANON_ACTOR.findall(path.read_text(encoding="utf-8", errors="replace")):
            found.append(f"{action} ({path.name})")
    return found


# Which call names count as an emit. `record` is ours; `audit` is the shape every real consumer
# converges on, because write-ahead ordering and a pseudonymous actor want to live in ONE helper
# rather than at forty call sites.
#
# This is configurable because it has to be. A team building on this repo copied the gate verbatim,
# wrapped the auditor in their own `audit(request, action, ...)` helper — the correct thing to do —
# and every one of their twenty-six actions was reported as never emitted. A gate that cries wolf
# on a whole codebase is not a strict gate; it is one people learn to disbelieve, and it stops
# working long before anyone switches it off.
_EMIT_CALLEES = tuple(
    n.strip() for n in os.environ.get("AUDIT_EMIT_CALLEES", "record,audit").split(",") if n.strip()
)
# `[^)]*?` — not `[^,)]*?`. The action is not always the FIRST argument: a wrapper is naturally
# written `audit(request, "tenant.suspended", ...)`, so the pattern has to be able to cross a comma
# to reach it. Refusing to was the bug's twin — it matched our own call shape and nothing else,
# which is exactly how a gate ends up validating only the code that inspired it.
_EMIT = re.compile(
    r"""(?:%s)\(\s*[^)]*?["']([a-z0-9_]+(?:\.[a-z0-9_]+)+)["']"""
    % "|".join(re.escape(n) for n in _EMIT_CALLEES)
)


def _without_docstrings(src: str) -> str:
    """Blank out module/class/function docstrings, keeping line count stable.

    The emit regex reads raw source, so a `record("x.y", ...)` shown as an EXAMPLE inside a docstring
    (auditable_actions.py teaches exactly that call) would be counted as a real emit — a phantom
    "emits an action it never declares". A docstring is never a real emit by definition, so removing
    it can only drop false positives, never hide a genuine call (those live in code, not prose).
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return src
    lines = src.splitlines(keepends=True)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = getattr(node, "body", [])
        if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            doc = body[0].value
            for i in range(doc.lineno - 1, (doc.end_lineno or doc.lineno)):
                if 0 <= i < len(lines):
                    lines[i] = "\n"
    return "".join(lines)


def emitted_actions(service: pathlib.Path) -> set[str]:
    """Action names this service actually passes to auditor.record() (or a configured wrapper).

    Read as source text, not by importing: the gate must never execute what it audits. It matches
    a literal string argument, which is the only form that can be checked statically — and is the
    form every call should use anyway, because a computed action name cannot be found by anyone
    searching the log either. Docstrings are stripped first (see _without_docstrings) so a call shown
    as an example in prose is not mistaken for a real emit.
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
        for match in _EMIT.finditer(_without_docstrings(path.read_text(encoding="utf-8"))):
            found.add(match.group(1))
    return found


def emits(name: str) -> bool:
    """Is this service deployed with a sink that actually reaches the log?"""
    values = VALUES / f"{name}.values.yaml"
    if not values.exists():
        return False
    text = values.read_text(encoding="utf-8")
    # [a-z_] not [a-z]: driver names include `in_process`. With [a-z] the match stopped at the
    # underscore and captured "in", which happened to pass (it is not "null") but for the wrong
    # reason — a driver literally named "null_something" would have been captured as "null" and
    # wrongly failed.
    match = re.search(r'^\s*AUDIT_SINK_DRIVER:\s*"?([a-z_]+)"?', text, re.MULTILINE)
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

            # R3. Declaring is a promise; emitting is what keeps it — enforced on EVERY declared
            # action, not just the critical ones. That limit used to be here, and it is exactly the
            # hole 68 non-critical actions fell through: they were declared, the sink was wired, the
            # config was right, and nothing recorded them — the compliance map read "witnessed" over
            # an empty chain and every check passed. The escape hatch below turns "this service
            # deliberately does not record X" from an oversight into a decision with a stated reason,
            # which is what keeps this strict without blocking legitimate work.
            emitted = emitted_actions(service)
            excused = unrecorded_actions(service)
            missing = sorted(a["action"] for a in actions
                             if a.get("action") not in emitted and a.get("action") not in excused)
            if missing:
                crit = {a["action"] for a in actions if a.get("critical")}
                problems.append(
                    f"  {name}: declares {len(missing)} action(s) that nothing ever records: "
                    f"{', '.join(missing)}\n"
                    f"      {'(' + ', '.join(sorted(set(missing) & crit)) + ' are CRITICAL) ' if set(missing) & crit else ''}"
                    f"The declaration, the sink and the config are all in place, so every check\n"
                    f"      passes while the chain stays empty. Fix: call `await\n"
                    f"      request.app.state.auditor.record(\"<action>\", ...)` at the point the\n"
                    f"      action happens — or, if this service genuinely does not record it, add\n"
                    f"      {{\"action\": ..., \"why\": ...}} to UNRECORDED_ACTIONS in\n"
                    f"      app/auditable_actions.py.")
            # A stale or bogus exclusion must not rot silently: it excuses a real gap by accident.
            stale = sorted(a for a in excused if a in emitted)
            if stale:
                problems.append(f"  {name}: UNRECORDED_ACTIONS excuses {', '.join(stale)}, which "
                                f"IS recorded — remove the stale exclusion.")
            undeclared_excuse = sorted(a for a in excused
                                       if a not in {x.get("action") for x in actions})
            if undeclared_excuse:
                problems.append(f"  {name}: UNRECORDED_ACTIONS lists {', '.join(undeclared_excuse)}, "
                                f"not declared in AUDITABLE_ACTIONS.")

            # R5. The other direction, which this gate never checked at all: an emit for an
            # action nothing declares. Adopted from a team building on this repo, who added it to
            # their copy and had it catch a real bug in their own code within minutes.
            #
            # It is not a style rule. Our Auditor raises DeclarationError on an undeclared action,
            # so every one of these is a 500 waiting for the first request that takes that path —
            # and it will be a privileged path, because those are the ones that record. Catching
            # it in CI costs nothing; catching it in production costs the action.
            undeclared = sorted(emitted - {a.get("action") for a in actions})
            if undeclared:
                problems.append(
                    f"  {name}: emits {len(undeclared)} action(s) it never declares:\n"
                    f"      {', '.join(undeclared)}\n"
                    f"      The Auditor raises DeclarationError on an undeclared action, so this\n"
                    f"      is a 500 waiting for the first caller. Fix: add it to\n"
                    f"      app/auditable_actions.py, or correct the name at the call site.")

            # R4. An entry that names nobody. See anonymous_emits() for why this is worse than a
            # missing entry rather than a lesser version of one.
            anon = anonymous_emits(service)
            if anon:
                problems.append(
                    f"  {name}: {len(anon)} action(s) recorded with an EMPTY actor on a\n"
                    f"      user-facing route: {', '.join(anon)}\n"
                    f"      The log will say the action happened and not who did it, which is the\n"
                    f"      one question it exists to answer. Fix: bind the principal —\n"
                    f"      `principal: Principal = Depends(_ADMIN)` instead of\n"
                    f"      `dependencies=[Depends(_ADMIN)]` — and pass actor=principal.subject.\n"
                    f"      Both spellings authorize identically; only one is attributable.")

    if problems:
        print("      audit trail is not wired to reality:")
        print("\n".join(problems))
        return 1

    print(f"      {wired} services declare auditable actions and are configured to record them")
    return 0


if __name__ == "__main__":
    sys.exit(main())
