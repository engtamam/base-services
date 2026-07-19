"""Erasure — connector. The DLQ is the largest pile of other people's PII the platform holds.

The decision under test is DELETE rather than redact, which is the opposite of webhook's. A DLQ row
exists to be RETRIED: a redacted one would be replayed into the customer's CRM with a pseudonym
where the email was, corrupting a downstream system we do not own in the name of privacy.
"""
from __future__ import annotations

import asyncio

from app.core.erasure import ConnectorErasure

_SALT = "s" * 32
_SUB = "alice@example.com"
_TEXT = frozenset({"error", "message"})


def run(c):
    return asyncio.run(c)


class _Store:
    def __init__(self, rows):
        self.rows = list(rows)
        self.deleted = []
        self.puts = []

    async def find(self, collection, filt, limit=100, after=None, sort=None):
        rows = [r for r in self.rows if all(r.get(k) == v for k, v in filt.items())]
        start = int(after or 0)
        page = rows[start:start + limit]
        nxt = start + len(page)
        return page, (str(nxt) if nxt < len(rows) else None)

    async def delete(self, collection, key):
        self.deleted.append(key)
        self.rows = [r for r in self.rows if r["id"] != key]

    async def put(self, collection, key, value):
        self.puts.append(key)


def _rec(id, email=_SUB, error="", tenant="acme"):
    return {"id": id, "tenant": tenant, "created": 1.0, "error": error,
            "record": {"properties": {"email": email, "firstname": "Alice"}}}


def _mk(store):
    return ConnectorErasure(store=store, pii_salt=_SALT, text_keys=_TEXT)


def test_a_dead_lettered_record_holding_the_subject_is_deleted_not_redacted():
    # Redacting would leave a row that gets RETRIED, writing a pseudonym into the customer's CRM.
    store = _Store([_rec("d1")])
    out = run(_mk(store).erase("acme", _SUB))
    assert store.deleted == ["d1"] and store.puts == []
    assert out["erased"] == ["dead_lettered_records:1"]


def test_a_record_naming_the_subject_only_in_its_error_is_caught():
    store = _Store([_rec("d1", email="other@example.com", error=f"failed to sync {_SUB}")])
    run(_mk(store).erase("acme", _SUB))
    assert store.deleted == ["d1"]


def test_another_persons_record_survives():
    store = _Store([_rec("mine"), _rec("theirs", email="bob@example.com")])
    run(_mk(store).erase("acme", _SUB))
    assert store.deleted == ["mine"]


def test_another_tenants_record_is_never_touched():
    store = _Store([_rec("theirs", tenant="other")])
    run(_mk(store).erase("acme", _SUB))
    assert store.deleted == []


def test_nothing_matching_acks_already_absent():
    store = _Store([_rec("d1", email="bob@example.com")])
    out = run(_mk(store).erase("acme", _SUB))
    assert out["already_absent"] is True and out["erased"] == []


def test_erasing_twice_is_a_no_op_that_still_acks():
    store = _Store([_rec("d1")])
    first = run(_mk(store).erase("acme", _SUB))
    second = run(_mk(store).erase("acme", _SUB))
    assert first["erased"] and second["erased"] == [] and second["already_absent"] is True


def test_the_ack_carries_a_pseudonym_not_the_person():
    out = run(_mk(_Store([_rec("d1")])).erase("acme", _SUB))
    assert out["subject_ref"].startswith("sub_") and _SUB not in out["subject_ref"]
