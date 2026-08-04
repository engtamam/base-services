"""The ticket-type workflow registry — a state machine built and validated from config (53.2).

The point of these tests is the NEGATIVE ones: an incoherent type must be refused at boot, not
discovered when a ticket can never be closed. And the shipped default config must itself be coherent.
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.core.workflow import WorkflowError, WorkflowRegistry


def _type(**over):
    base = {
        "key": "t", "label": {"en": "T"},
        "statuses": ["new", "done"], "start": "new",
        "transitions": {"new": ["done"]}, "terminal": ["done"],
    }
    base.update(over)
    return base


# ── the shipped default is coherent ───────────────────────────────────────────
def test_the_default_config_builds_and_is_coherent():
    reg = WorkflowRegistry.from_config(get_settings().ticket_types)
    assert "feedback" in reg.keys() and "support" in reg.keys()
    fb = reg.require("feedback")
    assert fb.start == "new"
    assert fb.can_transition("resolved", "in_progress")     # reopen
    assert fb.is_terminal("closed")
    assert "ref" in fb.required_fields


# ── incoherent types are refused at build ─────────────────────────────────────
def test_empty_config_is_refused():
    with pytest.raises(WorkflowError):
        WorkflowRegistry.from_config([])


def test_a_transition_to_an_unknown_status_is_refused():
    with pytest.raises(WorkflowError):
        WorkflowRegistry.from_config([_type(transitions={"new": ["ghost"]})])


def test_a_missing_terminal_is_refused():
    with pytest.raises(WorkflowError):
        WorkflowRegistry.from_config([_type(terminal=[])])


def test_a_terminal_with_outgoing_transitions_is_refused():
    with pytest.raises(WorkflowError):
        WorkflowRegistry.from_config([_type(
            statuses=["new", "done"], transitions={"new": ["done"], "done": ["new"]},
            terminal=["done"])])


def test_an_unreachable_status_is_refused():
    with pytest.raises(WorkflowError):
        WorkflowRegistry.from_config([_type(
            statuses=["new", "orphan", "done"], transitions={"new": ["done"]}, terminal=["done"])])


def test_a_start_outside_statuses_is_refused():
    with pytest.raises(WorkflowError):
        WorkflowRegistry.from_config([_type(start="nope")])


def test_a_duplicate_key_is_refused():
    with pytest.raises(WorkflowError):
        WorkflowRegistry.from_config([_type(key="dup"), _type(key="dup")])


def test_transition_capabilities_naming_unknown_status_is_refused():
    with pytest.raises(WorkflowError):
        WorkflowRegistry.from_config([_type(transition_capabilities={"ghost": "tickets.manage"})])


# ── the engine answers the questions the API will ask ─────────────────────────
def test_capability_for_defaults_to_the_operator_cap():
    reg = WorkflowRegistry.from_config([_type(transition_capabilities={"done": "tickets.close"})])
    t = reg.require("t")
    assert t.capability_for("done", "tickets.manage") == "tickets.close"
    assert t.capability_for("new", "tickets.manage") == "tickets.manage"   # undeclared -> default
