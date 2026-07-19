"""Every action this service declares must actually be emitted somewhere.

safety:audit-wiring enforces this for `critical` actions only, and that limit is deliberate — but
it is exactly why six declarations sat unemitted without anything going red: all six were routine.
consent.granted, dsar.created, dsar.verified, data_target.registered, ropa.recorded and
dsar.target_acked were all declared, all documented, and none of them ever written.

dsar.target_acked was the one that mattered. Completion is DERIVED from target acks, so it is the
state transition that closes a DSAR — and it was the transition with no record behind it. "Who
told us this target had erased its copy" was unanswerable.

This test is per-service and total, so it does not depend on how a future maintainer sets
`critical`. It reads the declaration as a literal and the emits as text, exactly as the gate does:
neither imports the service.
"""
from __future__ import annotations

import ast
import pathlib
import re

APP = pathlib.Path(__file__).resolve().parents[2] / "app"

# One or more dots — `catalog.draft.approved` is a legal action name, and a two-segment pattern
# would fail to see its emit and report a wired action as missing.
_EMIT = re.compile(r"""record\(\s*["']([a-z0-9_]+(?:\.[a-z0-9_]+)+)["']""")


def _declared() -> set[str]:
    tree = ast.parse((APP / "auditable_actions.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        target = None
        if isinstance(node, ast.AnnAssign):
            target = getattr(node.target, "id", "")
        elif isinstance(node, ast.Assign):
            target = getattr(node.targets[0], "id", "")
        if target == "AUDITABLE_ACTIONS" and node.value is not None:
            return {d["action"] for d in ast.literal_eval(node.value)}
    raise AssertionError("AUDITABLE_ACTIONS not found — the declaration is the contract")


def _emitted() -> set[str]:
    found: set[str] = set()
    for path in APP.rglob("*.py"):
        if "__pycache__" in str(path):
            continue
        found |= set(_EMIT.findall(path.read_text(encoding="utf-8", errors="replace")))
    return found


def test_nothing_is_declared_without_being_emitted():
    """A declaration nobody emits is a control that exists on paper. Every check passes, the
    catalog lists it, and the chain stays empty."""
    missing = sorted(_declared() - _emitted())
    assert missing == [], f"declared but never emitted: {missing}"


def test_nothing_is_emitted_without_being_declared():
    """The other direction, which the gate does not check at all. An undeclared emit raises
    DeclarationError at runtime — a 500 waiting for the first request that takes that path."""
    undeclared = sorted(_emitted() - _declared())
    assert undeclared == [], f"emitted but never declared: {undeclared}"


def test_the_dsar_ack_is_emitted_because_completion_is_derived_from_it():
    """Named on its own so a future edit deleting it fails with the reason, not just a count."""
    assert "dsar.target_acked" in _emitted()
