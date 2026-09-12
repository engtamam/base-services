"""Retention must not delete an audit record belonging to a subject under legal hold.

It did. enforce_retention deleted every expired event unconditionally, and this service could not
even ask whether a subject was held: its rows carry `actor` as a subject_ref — an HMAC with no
inverse — while compliance-service's hold-check takes a raw subject. There was nothing to ask
WITH, and handing this service the salt to re-derive one would defeat the pseudonymisation the
whole design rests on.

So the question is inverted: fetch every held REF for a partition once, intersect locally. Both
sides derive subject_ref from the same frozen product PII_SALT, so no raw identifier crosses the
wire in either direction.

Every test here is a way of asking whether a purge can delete evidence somebody is legally
required to keep.
"""
from __future__ import annotations

import asyncio

import pytest


def run(c):
    return asyncio.run(c)


class _Repo:
    """Enough AuditRepository to exercise purge: events, deletes, head."""

    def __init__(self, events, partitions=("global",)):
        self.events = list(events)
        self._partitions = list(partitions)
        self.deleted: list[str] = []
        self._version = 0

    async def iterate(self, partition):
        return [e for e in self.events if e.get("partition", "global") == partition]

    async def delete(self, event_id):
        self.deleted.append(event_id)
        self.events = [e for e in self.events if e["id"] != event_id]

    async def replace(self, e):
        return None

    async def head_versioned(self, partition):
        # purge pins its head-move to the version observed here; no concurrent append in these
        # single-threaded tests, so the first set_head_versioned() always wins.
        evs = [e for e in self.events if e.get("partition", "global") == partition]
        seq = max((e["seq"] for e in evs), default=0)
        return seq, "", self._version

    async def set_head_versioned(self, partition, expected_version, seq, prev):
        if expected_version != self._version:
            return False
        self._version += 1
        return True

    async def set_head(self, partition, seq, prev):
        return None

    async def partitions(self):
        return list(self._partitions)


class _Integrity:
    def seal(self, prev, e):
        return f"h{e['seq']}"


def _svc(repo):
    from app.core.audit_service import AuditService
    svc = AuditService.__new__(AuditService)
    svc._repo = repo
    svc._integrity = _Integrity()
    svc._pii = None
    svc._mode = "tenant"
    svc.record = _noop_record
    return svc


async def _noop_record(**kw):
    return None


def _event(seq, ts, actor, partition="global"):
    return {"id": f"e{seq}", "seq": seq, "ts": ts, "actor": actor, "partition": partition}


OLD, NEW = "2020-01-01", "2030-01-01"
# 1 day, so the cutoff lands near today and OLD really is expired. With a large value the
# cutoff predates OLD, nothing expires, and every "purged == 0" assertion below would hold for
# the wrong reason — passing while proving nothing.
_RETAIN_1_DAY = 1
HELD = "sub_" + "a" * 32
FREE = "sub_" + "b" * 32


# ── the purge itself ───────────────────────────────────────────────────────────
def test_an_expired_event_for_a_HELD_subject_is_kept():
    """THE test. Before this, it was deleted like any other expired row."""
    repo = _Repo([_event(1, OLD, HELD), _event(2, OLD, FREE)])
    out = run(_svc(repo).purge("2025-01-01", "global", held_refs=frozenset({HELD})))

    assert out["purged"] == 1
    assert out["held"] == 1
    assert "e1" not in repo.deleted, "deleted an audit record under legal hold"
    assert "e2" in repo.deleted


def test_the_held_count_is_REPORTED_not_silently_skipped():
    """retention.py's rule: a held row is reported as retained. An operator reading
    'purged: 500' has to know 12 were preserved under hold — that number is the difference
    between a lawful purge and spoliation."""
    repo = _Repo([_event(1, OLD, HELD)])
    assert run(_svc(repo).purge("2025-01-01", "global", held_refs=frozenset({HELD})))["held"] == 1


def test_no_hold_set_means_no_protection_which_is_the_old_behaviour():
    repo = _Repo([_event(1, OLD, HELD)])
    out = run(_svc(repo).purge("2025-01-01", "global", held_refs=None))
    assert out["purged"] == 1 and out["held"] == 0


def test_an_UNEXPIRED_event_is_untouched_either_way():
    repo = _Repo([_event(1, NEW, HELD), _event(2, NEW, FREE)])
    out = run(_svc(repo).purge("2025-01-01", "global", held_refs=frozenset({HELD})))
    assert out["purged"] == 0 and repo.deleted == []


# ── the sweep, where fail-closed lives ─────────────────────────────────────────
def test_an_INCOMPLETE_hold_list_skips_the_partition_instead_of_purging():
    """Fail closed, and deliberately the opposite of this codebase's usual degrade-and-continue.
    Carrying on here deletes records that may be under hold, which is not recoverable."""
    repo = _Repo([_event(1, OLD, HELD)], partitions=("acme",))

    async def holds(tenant):
        return {"refs": [HELD], "complete": False}      # truncated

    out = run(_svc(repo).enforce_retention(_RETAIN_1_DAY, holds=holds))
    assert out["purged"] == 0
    assert repo.deleted == [], "purged on a partial hold list"
    assert out["skipped"] and out["skipped"][0]["partition"] == "acme"


def test_an_UNAVAILABLE_hold_list_also_skips():
    """None and incomplete mean the same thing to the caller: we do not know what is protected."""
    repo = _Repo([_event(1, OLD, HELD)], partitions=("acme",))

    async def holds(tenant):
        return None

    out = run(_svc(repo).enforce_retention(_RETAIN_1_DAY, holds=holds))
    assert out["purged"] == 0 and repo.deleted == []


def test_a_complete_hold_list_purges_the_unheld_and_keeps_the_held():
    repo = _Repo([_event(1, OLD, HELD, "acme"), _event(2, OLD, FREE, "acme")],
                 partitions=("acme",))

    async def holds(tenant):
        return {"refs": [HELD], "complete": True}

    out = run(_svc(repo).enforce_retention(_RETAIN_1_DAY, holds=holds))
    assert out["purged"] == 1 and out["held"] == 1
    assert out["skipped"] == []
    assert out["hold_protected"] is True


def test_hold_protected_is_stated_when_no_gate_is_configured():
    """Otherwise an operator cannot tell a run that protected held data from one that never
    looked — and both print a reassuring 'purged: N'."""
    repo = _Repo([_event(1, OLD, HELD)])
    out = run(_svc(repo).enforce_retention(_RETAIN_1_DAY, holds=None))
    assert out["hold_protected"] is False


# ── the null adapter, where a fake control would hide ──────────────────────────
def test_the_null_hold_refs_returns_None_not_an_empty_set():
    """An empty set and an unknown set look identical in the data and are OPPOSITES in meaning:
    empty asserts 'nobody is held' and would let the purge delete everything expired."""
    from app.adapters.hold_refs.null import NullHoldRefs

    assert run(NullHoldRefs().for_tenant("acme")) is None


def test_the_http_adapter_treats_a_missing_complete_flag_as_incomplete(monkeypatch):
    """Assuming completeness is exactly the assumption that deletes held evidence."""
    import json
    import urllib.request

    from app.adapters.hold_refs.http import HttpHoldRefs

    class _Resp:
        status = 200

        def read(self):
            return json.dumps({"refs": [HELD]}).encode()      # no 'complete'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: _Resp())
    out = run(HttpHoldRefs("http://compliance:8000", "t" * 40).for_tenant("acme"))
    assert out["complete"] is False
