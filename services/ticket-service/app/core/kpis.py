"""Ticket KPIs — DERIVED from the append-only event log, never stored and never guessed.

The log is already the truth: `created` opens it, `assigned` records who received it and when, the first
`replied` with data.by=="operator" is the first real response, `escalated` marks an escalation, and the
`status_changed` timeline shows resolution. A stipend decision (spec §8) built on these reads a fact the
service can always re-derive from its own log — not a metric mutated in place that can drift from what
actually happened.

Pure: (ticket_row, events) -> dict. No I/O, no clock — the timestamps are the events' own. So it is
trivially testable and identical for the internal read, an operator dashboard, or an SLA report.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any


def _ts(iso: str) -> float | None:
    if not iso or not isinstance(iso, str):
        return None
    try:
        return _dt.datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


def _seconds_between(a: str, b: str) -> float | None:
    ta, tb = _ts(a), _ts(b)
    return round(tb - ta, 3) if (ta is not None and tb is not None and tb >= ta) else None


def ticket_kpis(ticket: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    """Support-performance facts for one ticket, derived from its events (which MUST be ordered by seq).

    Carries timings and structure, NEVER free text: no subject, no reply bodies — a party granted a
    scoped view sees HOW support performed, not the customer content, which may not be theirs to read.
    """
    created_at = ticket.get("created", "")
    ordered = sorted(events, key=lambda e: e.get("seq", 0))

    assigned_at = assignee_ref = ""
    first_response_at = ""
    escalated_at = escalation_level = ""
    resolved_at = ""
    reply_count = 0
    operator_reply_count = 0
    rating = None

    for e in ordered:
        kind = e.get("kind", "")
        data = e.get("data") or {}
        at = e.get("created", "")
        if kind == "assigned" and not assigned_at:
            assigned_at, assignee_ref = at, data.get("assignee", "") or e.get("actor_ref", "")
        elif kind == "replied":
            reply_count += 1
            if data.get("by") == "operator":
                operator_reply_count += 1
                if not first_response_at:
                    first_response_at = at              # the FIRST real answer to the customer
        elif kind == "escalated" and not escalated_at:
            escalated_at, escalation_level = at, str(data.get("level", "") or "")
        elif kind == "status_changed":
            resolved_at = at if data.get("resolved") or data.get("terminal") else resolved_at
        elif kind == "rated":
            rating = data.get("score", rating)

    return {
        "created_at": created_at,
        "assigned_at": assigned_at,
        "assignee_ref": assignee_ref,
        "first_response_at": first_response_at,
        # The headline SLA number a stipend keys on: how fast the FIRST real answer came.
        "first_response_seconds": _seconds_between(created_at, first_response_at),
        "escalated": bool(escalated_at),
        "escalated_at": escalated_at,
        "escalation_level": escalation_level,
        "resolved_at": resolved_at,
        "resolution_seconds": _seconds_between(created_at, resolved_at),
        "reply_count": reply_count,
        "operator_reply_count": operator_reply_count,
        "rating": rating,
        "status": ticket.get("status", ""),
    }
