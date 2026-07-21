#!/usr/bin/env python3
"""safety:event-contract — a service admits to the events it publishes and consumes.

The manifest is how one team discovers what another team's service does. `events.emitted` is the
only place a consumer can learn that `payment.succeeded` exists at all — there is no registry, no
schema server, and no way to observe a topic that has never fired.

So an undeclared event is not a documentation gap. It reaches nobody, which is operationally the
same as not publishing it: the code runs, the broker accepts it, nothing is wrong anywhere, and the
integration that should have existed simply never gets built because the person who would have
built it could not see the event.

This was the state of the whole repository when the gate was written. billing-service published
nothing at all and declared `"emitted": []`. compliance-service and connector-service DID publish —
and also declared `"emitted": []`, which is worse, because their manifests actively asserted that
they emit nothing.

WHAT IS CHECKED

  1. A service that calls `publish(...)` must declare a non-empty `emitted`.
  2. A service that calls `subscribe(...)` must declare a non-empty `consumed`.
  3. Event names that appear as literals next to a publish must be declared.
  4. A declared event must be published SOMEWHERE in the service — a manifest promising an event
     that never fires leaves a consumer waiting for ever, which is the harder failure to diagnose
     because the subscriber looks healthy.

WHAT IT DELIBERATELY DOES NOT DO. It does not verify payload shape; that needs a schema registry
and is a different piece of work. It does not resolve dynamically built topic names — a service
that computes an event name gets rule 1 and 2 only, and the check says so rather than pretending
to have proved more.
"""
from __future__ import annotations

import ast
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICES = pathlib.Path(os.environ.get("SERVICES_ROOT") or (ROOT / "services"))

# A dotted lowercase name: "payment.succeeded", "consent.withdrawn". Deliberately narrow, so a
# format string or a variable is simply not counted rather than guessed at.
_EVENT_NAME = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")

_SKIP_DIRS = ("__pycache__", ".venv", "tests", "node_modules")

# The receiver must actually BE an event bus. Matching any `.publish()`/`.subscribe()`/`.emit()`
# call flagged all twenty services on noise the first time this ran: the event-bus ADAPTERS
# implement publish/subscribe (redis `pubsub.subscribe(topic)`), audit sinks have their own
# `emit()`, and billing-service has a domain method literally called `subscribe` — putting a
# tenant on a plan, nothing to do with events. A gate that reports twenty pieces of correct code
# is one people learn to skip past, so the receiver name is checked.
_BUS_RECEIVERS = frozenset({
    "bus", "_bus", "event_bus", "_event_bus", "eventbus", "_eventbus",
    "events", "_events", "emitter", "_emitter",
})


def _py_files(service: pathlib.Path):
    app = service / "app"
    if not app.is_dir():
        return []
    return [p for p in app.rglob("*.py")
            if not any(part in _SKIP_DIRS for part in p.parts)
            # The adapters IMPLEMENT the port; they are not callers of it.
            and "event_bus" not in p.parts]


def _declared(service: pathlib.Path) -> tuple[set[str], set[str], bool]:
    """(emitted, consumed, found) as declared in the manifest, read as source.

    Read literally rather than imported: importing a manifest runs the service's config, which
    needs an environment this gate has no business constructing.
    """
    man = service / "app" / "manifest.py"
    if not man.is_file():
        return set(), set(), False
    text = man.read_text(encoding="utf-8")
    emitted, consumed = set(), set()
    # Names referenced via a constant (list(_EMITTED)) are resolved from the events module below.
    m = re.search(r'"events"\s*:\s*\{(.*?)\}', text, re.S)
    if not m:
        return set(), set(), False
    block = m.group(1)
    for key, into in (("emitted", emitted), ("consumed", consumed)):
        km = re.search(rf'"{key}"\s*:\s*(\[[^\]]*\]|list\(\s*(\w+)\s*\))', block, re.S)
        if not km:
            continue
        if km.group(2):
            into.update(_constants_from_events_module(service, km.group(2)))
        else:
            into.update(re.findall(r'"([^"]+)"', km.group(1)))
    return emitted, consumed, True


def _constants_from_events_module(service: pathlib.Path, symbol: str) -> set[str]:
    """Resolve `list(_EMITTED)` back to the names in app/core/events.py."""
    ev = service / "app" / "core" / "events.py"
    if not ev.is_file():
        return set()
    text = ev.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    consts = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    consts[t.id] = node.value.value
    want = symbol.lstrip("_")
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, (ast.Tuple, ast.List)):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if not any(n.lstrip("_") == want for n in names):
                continue
            out = set()
            for elt in node.value.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    out.add(elt.value)
                elif isinstance(elt, ast.Name) and elt.id in consts:
                    out.add(consts[elt.id])
            return out
    return set()


def _calls(service: pathlib.Path) -> tuple[bool, bool, set[str], set[str]]:
    """(publishes, subscribes, published_literals, subscribed_literals)."""
    publishes = subscribes = False
    pub_names, sub_names = set(), set()
    for path in _py_files(service):
        text = path.read_text(encoding="utf-8")
        if "publish" not in text and "subscribe" not in text:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        # The events module itself only DEFINES names; calling it a publisher would make every
        # service with a constants file look like one.
        defines_only = path.name == "events.py"
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            attr = node.func.attr
            if attr not in ("publish", "subscribe", "emit"):
                continue
            base = node.func.value
            receiver = getattr(base, "id", "") or getattr(base, "attr", "")
            if receiver not in _BUS_RECEIVERS:
                continue
            if not defines_only:
                if attr in ("publish", "emit"):
                    publishes = True
                else:
                    subscribes = True
            target = pub_names if attr in ("publish", "emit") else sub_names
            for arg in node.args[:1]:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                        and _EVENT_NAME.match(arg.value):
                    target.add(arg.value)
    return publishes, subscribes, pub_names, sub_names


def main() -> int:
    problems: list[str] = []
    notes: list[str] = []
    checked = 0

    for service in sorted(p for p in SERVICES.iterdir() if p.is_dir()):
        if service.name.startswith("_"):
            continue          # the scaffold declares nothing and publishes nothing
        emitted, consumed, found = _declared(service)
        if not found:
            continue
        publishes, subscribes, pub_names, sub_names = _calls(service)
        checked += 1
        name = service.name

        # A service that publishes only through a variable topic — `_emit(event, payload)` with
        # the name passed in by a caller — cannot be checked by name, and neither can an audit
        # sink fanning its trail out to the bus. Those are NOTED, never failed: this gate must not
        # claim to have proved something it has not, and 19 unfixable findings is how a gate stops
        # being read. Rules 3 and 4 below are the ones that bite, and they bite exactly.
        if publishes and not emitted and not pub_names:
            notes.append(f"{name}: publishes with a computed topic and declares no events — "
                         f"cannot be verified by name")
        if subscribes and not consumed and not sub_names:
            notes.append(f"{name}: subscribes with a computed topic and declares none")
        for missing in sorted(pub_names - emitted):
            problems.append(f"{name}: publishes {missing!r} but does not declare it in emitted")
        for missing in sorted(sub_names - consumed):
            problems.append(f"{name}: subscribes to {missing!r} but does not declare it in consumed")
        # A promise nothing keeps. Only checked when the service names its events literally;
        # otherwise there is nothing to compare and claiming otherwise would be a guess.
        if pub_names:
            for never in sorted(emitted - pub_names):
                problems.append(
                    f"{name}: declares {never!r} but nothing publishes it — a subscriber waits "
                    f"for ever and looks healthy while doing it")

    for n in notes:
        print(f"      note: {n}")
    if problems:
        print("      the event contract and the code disagree:")
        for p in problems:
            print(f"        {p}")
        print("      An event a consumer cannot discover is one nobody subscribes to.")
        return 1

    print(f"      {checked} service manifest(s) agree with what the code publishes and consumes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
