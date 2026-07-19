"""Erasure — webhook, and the blob walker underneath it.

The blob tests are where the real risk lives. Matching too loosely rewrites data belonging to other
people; matching too tightly leaves the subject in place. Both are silent, so both get tests.
"""
from __future__ import annotations

import asyncio

import pytest

from app.core.blob import MAX_DEPTH, redact
from app.core.erasure import WebhookErasure

_SALT = "s" * 32
_SUB = "alice@example.com"
_REF = "sub_pseudonym"
_TEXT = frozenset({"body", "response_body"})


def run(c):
    return asyncio.run(c)


# ── the blob walker ───────────────────────────────────────────────────────────
def test_an_exact_value_match_is_redacted():
    out, hits = redact({"email": _SUB}, _SUB, _REF)
    assert out == {"email": _REF} and hits == 1


def test_it_finds_the_person_however_deeply_nested():
    blob = {"a": [{"b": {"c": [_SUB]}}]}
    out, hits = redact(blob, _SUB, _REF)
    assert out == {"a": [{"b": {"c": [_REF]}}]} and hits == 1


def test_a_field_that_merely_contains_the_id_is_left_alone():
    # THE over-matching test. Redacting these would rewrite unrelated data — including other
    # people's records that happen to reference this subject.
    blob = {"note": f"forwarded from {_SUB}", "url": f"https://x/{_SUB}/avatar"}
    out, hits = redact(blob, _SUB, _REF)
    assert out == blob and hits == 0


def test_substring_matching_happens_only_under_a_configured_text_key():
    blob = {"body": f"Hi {_SUB}, welcome", "note": f"Hi {_SUB}"}
    out, hits = redact(blob, _SUB, _REF, _TEXT)
    assert out["body"] == f"Hi {_REF}, welcome"     # configured -> substring
    assert out["note"] == f"Hi {_SUB}"              # not configured -> untouched
    assert hits == 1


def test_the_shape_survives_redaction():
    # A payload with a MISSING key can crash a consumer replaying it. An erasure must not become
    # an outage, so the key stays and only the value changes.
    blob = {"user": {"email": _SUB, "plan": "pro"}}
    out, _ = redact(blob, _SUB, _REF)
    assert set(out["user"]) == {"email", "plan"} and out["user"]["plan"] == "pro"


def test_it_does_not_mutate_the_input():
    blob = {"email": _SUB}
    redact(blob, _SUB, _REF)
    assert blob == {"email": _SUB}


def test_absurd_nesting_terminates():
    blob: dict = {"v": _SUB}
    for _ in range(MAX_DEPTH + 10):
        blob = {"n": blob}
    redact(blob, _SUB, _REF)      # must return rather than recurse for ever


def test_non_string_leaves_are_untouched():
    blob = {"count": 7, "ok": True, "nothing": None}
    out, hits = redact(blob, _SUB, _REF)
    assert out == blob and hits == 0


# ── the service ───────────────────────────────────────────────────────────────
class _Store:
    def __init__(self, data):
        self.data = {k: list(v) for k, v in data.items()}
        self.puts = []

    async def find(self, collection, filt, limit=100, after=None, sort=None):
        rows = [r for r in self.data.get(collection, [])
                if all(r.get(k) == v for k, v in filt.items())]
        start = int(after or 0)
        page = rows[start:start + limit]
        nxt = start + len(page)
        return page, (str(nxt) if nxt < len(rows) else None)

    async def put(self, collection, key, value):
        self.puts.append((collection, key))
        self.data[collection] = [value if r["id"] == key else r for r in self.data[collection]]


def _mk(store):
    return WebhookErasure(store=store, pii_salt=_SALT, text_keys=_TEXT)


def test_the_subject_is_redacted_out_of_event_payloads():
    store = _Store({"events": [{"id": "e1", "tenant": "acme", "created": "1",
                                "payload": {"user": {"email": _SUB}}}]})
    out = run(_mk(store).erase("acme", _SUB))
    assert store.data["events"][0]["payload"]["user"]["email"].startswith("sub_")
    assert out["erased"] == ["events:1"]


def test_another_tenants_rows_are_never_rewritten():
    # One customer's DSAR must not reach into another customer's data.
    store = _Store({"events": [
        {"id": "mine", "tenant": "acme", "created": "1", "payload": {"email": _SUB}},
        {"id": "theirs", "tenant": "other", "created": "1", "payload": {"email": _SUB}},
    ]})
    run(_mk(store).erase("acme", _SUB))
    theirs = [r for r in store.data["events"] if r["id"] == "theirs"][0]
    assert theirs["payload"]["email"] == _SUB


def test_delivery_history_is_redacted_not_deleted():
    # The row is how an operator answers "did it ever arrive?". Only the person leaves.
    store = _Store({"deliveries": [{"id": "d1", "tenant": "acme", "created": "1",
                                    "response_body": f"ok for {_SUB}", "response_code": 200}]})
    run(_mk(store).erase("acme", _SUB))
    row = store.data["deliveries"][0]
    assert row["response_code"] == 200 and _SUB not in row["response_body"]


def test_inbound_events_are_walked_even_though_they_are_not_tenant_keyed():
    store = _Store({"inbound": [{"id": "i1", "created": "1",
                                 "payload": {"customer": {"email": _SUB}}}]})
    out = run(_mk(store).erase("acme", _SUB))
    assert "inbound:1" in out["erased"]


def test_nothing_matching_acks_already_absent():
    store = _Store({"events": [{"id": "e1", "tenant": "acme", "created": "1",
                                "payload": {"email": "someone@else.com"}}]})
    out = run(_mk(store).erase("acme", _SUB))
    assert out["already_absent"] is True and store.puts == []


def test_erasing_twice_is_a_no_op_that_still_acks():
    store = _Store({"events": [{"id": "e1", "tenant": "acme", "created": "1",
                                "payload": {"email": _SUB}}]})
    first = run(_mk(store).erase("acme", _SUB))
    second = run(_mk(store).erase("acme", _SUB))
    assert first["erased"] == ["events:1"]
    assert second["erased"] == [] and second["already_absent"] is True


def test_a_webhook_payload_is_never_retained_under_a_basis():
    # Nothing here has a legal duty behind it; inventing one would be indefensible.
    store = _Store({"events": [{"id": "e1", "tenant": "acme", "created": "1",
                                "payload": {"email": _SUB}}]})
    assert run(_mk(store).erase("acme", _SUB))["retained"] == []
