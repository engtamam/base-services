"""Erasure — job. The queue-specific rule is what these tests are really for.

Redacting a job that has not run yet is not enough: the job still runs. An erasure that leaves work
scheduled against the erased person has not erased them, it has queued one more contact with them.
"""
from __future__ import annotations

import asyncio

from app.core.erasure import JobErasure
from app.core.models import CANCELED, DONE, LEASED, QUEUED

_SALT = "s" * 32
_SUB = "alice@example.com"
_TEXT = frozenset({"body", "message"})


def run(c):
    return asyncio.run(c)


class _Store:
    def __init__(self, data):
        self.data = {k: list(v) for k, v in data.items()}

    async def find(self, collection, filt, limit=100, after=None, sort=None):
        rows = [r for r in self.data.get(collection, [])
                if all(r.get(k) == v for k, v in filt.items())]
        start = int(after or 0)
        page = rows[start:start + limit]
        nxt = start + len(page)
        return page, (str(nxt) if nxt < len(rows) else None)

    async def put(self, collection, key, value):
        self.data[collection] = [value if r["id"] == key else r for r in self.data[collection]]


def _job(id, status=QUEUED, payload=None, result=None):
    return {"id": id, "tenant": "acme", "created": 1.0, "status": status,
            "payload": payload if payload is not None else {"to": _SUB},
            "result": result or {}}


def _mk(store):
    return JobErasure(store=store, pii_salt=_SALT, text_keys=_TEXT)


def test_a_queued_job_mentioning_the_subject_is_cancelled():
    # THE rule. A redacted-but-still-queued job either runs against a pseudonym or still reaches
    # the person we were told to forget.
    store = _Store({"jobs": [_job("j1", QUEUED)]})
    out = run(_mk(store).erase("acme", _SUB))
    assert store.data["jobs"][0]["status"] == CANCELED
    assert "canceled_pending_work:1" in out["erased"]


def test_a_leased_job_is_cancelled_too():
    store = _Store({"jobs": [_job("j1", LEASED)]})
    run(_mk(store).erase("acme", _SUB))
    assert store.data["jobs"][0]["status"] == CANCELED


def test_a_finished_job_is_redacted_but_left_finished():
    # Rewriting history to "canceled" would falsify an operational record of work that DID run.
    store = _Store({"jobs": [_job("j1", DONE, result={"sent_to": _SUB})]})
    run(_mk(store).erase("acme", _SUB))
    row = store.data["jobs"][0]
    assert row["status"] == DONE
    assert row["result"]["sent_to"].startswith("sub_")


def test_the_cancellation_records_why():
    store = _Store({"jobs": [_job("j1", QUEUED)]})
    run(_mk(store).erase("acme", _SUB))
    assert "erasure" in store.data["jobs"][0]["cancel_reason"]


def test_a_recurring_schedule_is_disabled():
    # A cron that emails a deleted user every month is the most visible possible DSAR failure.
    store = _Store({"schedules": [{"id": "s1", "tenant": "acme", "created": 1.0,
                                   "enabled": True, "payload": {"to": _SUB}}]})
    run(_mk(store).erase("acme", _SUB))
    assert store.data["schedules"][0]["enabled"] is False


def test_another_persons_job_is_untouched():
    store = _Store({"jobs": [_job("mine"), _job("theirs", payload={"to": "bob@example.com"})]})
    run(_mk(store).erase("acme", _SUB))
    theirs = [r for r in store.data["jobs"] if r["id"] == "theirs"][0]
    assert theirs["status"] == QUEUED and theirs["payload"]["to"] == "bob@example.com"


def test_nothing_matching_acks_already_absent():
    store = _Store({"jobs": [_job("j1", payload={"to": "bob@example.com"})]})
    out = run(_mk(store).erase("acme", _SUB))
    assert out["already_absent"] is True and out["erased"] == []


def test_erasing_twice_is_a_no_op_that_still_acks():
    store = _Store({"jobs": [_job("j1")]})
    first = run(_mk(store).erase("acme", _SUB))
    second = run(_mk(store).erase("acme", _SUB))
    assert first["erased"] and second["erased"] == [] and second["already_absent"] is True
