"""Reading the audit log is itself on the audit log.

This service's whole job is answering "who did what" about everyone else, and it could not answer
it about itself: query and export left no trace. An unlogged read is how someone finds out what we
know about them, or checks whether their own tracks are visible, and walks away clean.

The interesting part is that the two reads are NOT treated the same, and the split is the point of
this file.
"""
from __future__ import annotations

import ast
import pathlib

APP = pathlib.Path(__file__).resolve().parents[2] / "app"


def _declared() -> dict[str, dict]:
    tree = ast.parse((APP / "auditable_actions.py").read_text(encoding="utf-8"))
    for node in tree.body:
        target = getattr(node, "target", None) or (getattr(node, "targets", [None]) or [None])[0]
        if getattr(target, "id", "") == "AUDITABLE_ACTIONS":
            return {d["action"]: d for d in ast.literal_eval(node.value)}
    raise AssertionError("AUDITABLE_ACTIONS not found")


def _routes() -> str:
    return (APP / "api" / "v1" / "audit.py").read_text(encoding="utf-8")


def test_both_reads_are_declared():
    d = _declared()
    assert "audit.queried" in d
    assert "audit.exported" in d


def test_both_reads_are_actually_emitted():
    """Declaring without emitting is the defect that shipped six times in compliance-service."""
    src = _routes()
    assert '"audit.queried"' in src
    assert '"audit.exported"' in src


def test_a_query_is_NOT_critical():
    """Critical means "refuse the action when the log is down". Refusing READS during an audit
    outage locks investigators out of the log at the exact moment something is going wrong, and a
    read changes nothing — so the safer failure is to serve it and lose one access record."""
    assert _declared()["audit.queried"]["critical"] is False


def test_an_export_IS_critical():
    """The opposite trade, for the opposite reason. Export takes the log out in BULK, which is the
    shape of exfiltration rather than investigation. An unrecorded bulk extraction of every action
    on the platform is worse than a refused one."""
    entry = _declared()["audit.exported"]
    assert entry["critical"] is True
    assert entry.get("why"), "a critical action must say why — it refuses live requests"


def test_the_export_records_BEFORE_it_reads():
    """Write-ahead, because it is critical: recording afterwards would mean the export had already
    left the building by the time we failed to witness it."""
    src = _routes()
    emit = src.index('"audit.exported"')
    read = src.index("await build_audit(request).export(")
    assert emit < read, "the export is served before it is recorded"


def test_neither_read_is_recorded_anonymously():
    """Both routes already bind the principal, so there is no excuse for an empty actor — and an
    access record that does not name the reader answers nothing at all."""
    src = _routes()
    for action in ('"audit.queried"', '"audit.exported"'):
        tail = src[src.index(action): src.index(action) + 400]
        assert "actor=principal.subject" in tail
        assert 'actor=""' not in tail
