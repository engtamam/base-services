"""The auditable-action declarations, and the validation that makes them mean something.

Both mistakes caught here are silent otherwise, and both surface only when an investigation needs
a record that is not there — which is the worst possible time to learn about them.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib

import pytest

from app.auditable_actions import AUDITABLE_ACTIONS
from app.core.audit import Auditor
from app.core.auditable import (SOURCES, DeclarationError, critical_actions, declared_actions,
                                validate)
from app.ports.audit_sink import ACCEPTED, AuditReceipt

_DECL = pathlib.Path(__file__).resolve().parents[2] / "app" / "auditable_actions.py"


def run(c):
    return asyncio.run(c)


def _ok(**kw):
    base = {"action": "user.suspended", "source": "admin", "critical": False, "resource": "user"}
    base.update(kw)
    return [base]


# ── the file itself ───────────────────────────────────────────────────────────
def test_the_declaration_is_a_pure_literal():
    """The gate reads this with ast.literal_eval so it never has to IMPORT (and therefore execute)
    a service to check it. An f-string or a variable reference breaks the gate, not just this file."""
    tree = ast.parse(_DECL.read_text(encoding="utf-8"))
    node = next(n for n in tree.body
                if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", "") == "AUDITABLE_ACTIONS")
    ast.literal_eval(node.value)          # raises if anything is computed


def test_the_shipped_examples_are_valid():
    validate(AUDITABLE_ACTIONS, "verification-service")


def test_the_template_shows_the_most_missed_source():
    """internal actions are the category people forget — "this service erased a person's data
    because a DSAR told it to" appears in no panel and no catalog. The example must show one."""
    assert any(a["source"] == "internal" for a in AUDITABLE_ACTIONS)


def test_the_template_shows_both_criticalities():
    assert {a["critical"] for a in AUDITABLE_ACTIONS} == {True, False}


# ── validation ────────────────────────────────────────────────────────────────
def test_a_flat_action_name_is_refused():
    # The dot lets an investigator query a whole family. A flat name is findable only if you
    # already know it exists.
    with pytest.raises(DeclarationError, match="dotted"):
        validate(_ok(action="suspenduser"), "svc")


def test_a_duplicate_action_is_refused():
    # Two declarations mean two criticality answers, and which applies would depend on list order.
    with pytest.raises(DeclarationError, match="twice"):
        validate(_ok() + _ok(), "svc")


@pytest.mark.parametrize("bad", ["panel", "", None, "ADMIN"])
def test_an_unknown_source_is_refused(bad):
    with pytest.raises(DeclarationError, match="source"):
        validate(_ok(source=bad), "svc")


def test_every_valid_source_is_accepted():
    for source in SOURCES:
        validate(_ok(source=source), "svc")


@pytest.mark.parametrize("bad", [None, 1, "yes", "", 0])
def test_criticality_must_be_an_explicit_boolean(bad):
    """Truthiness must not decide whether a request is refused when the log is down."""
    with pytest.raises(DeclarationError, match="critical"):
        validate(_ok(critical=bad), "svc")


def test_a_critical_action_without_a_reason_is_refused():
    # critical=True means "refuse the request when audit is down" — an availability decision. The
    # `why` is what forces it to be made on purpose rather than by accident.
    with pytest.raises(DeclarationError, match="no 'why'"):
        validate(_ok(critical=True), "svc")
    with pytest.raises(DeclarationError, match="no 'why'"):
        validate(_ok(critical=True, why="   "), "svc")


def test_a_critical_action_with_a_reason_passes():
    validate(_ok(critical=True, why="an unattributable grant looks exactly like an attacker's"),
             "svc")


def test_the_error_names_the_service_and_the_action():
    # These fire at boot across 18 services; a message without both sends someone hunting.
    with pytest.raises(DeclarationError, match="billing-service.*invoice.voided"):
        validate(_ok(action="invoice.voided", critical=True), "billing-service")


# ── the sets it produces ──────────────────────────────────────────────────────
def test_criticality_and_declaration_sets():
    actions = _ok() + _ok(action="permission.granted", critical=True, why="because")
    assert critical_actions(actions) == {"permission.granted"}
    assert declared_actions(actions) == {"user.suspended", "permission.granted"}


# ── the Auditor honours the declaration ───────────────────────────────────────
def test_emitting_an_undeclared_action_is_a_loud_error():
    """An action emitted but never declared is invisible to the gate AND to the investigator
    searching the log — audited in name only."""
    class _Sink:
        driver = "fake"

        async def emit(self, event):
            return AuditReceipt(status=ACCEPTED, hash="h")

    auditor = Auditor(sink=_Sink(), service="svc", declared_actions=frozenset({"user.suspended"}))
    run(auditor.record("user.suspended"))                       # declared: fine
    with pytest.raises(DeclarationError, match="not declared"):
        run(auditor.record("user.deleted"))
