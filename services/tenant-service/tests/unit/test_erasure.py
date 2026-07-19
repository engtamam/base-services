"""Erasure — tenant. Organisations, not people, plus the one field that is an exception."""
from __future__ import annotations

import asyncio

from app.core.erasure import TenantErasure

_SALT = "s" * 32
_SUB = "founder@acme.com"


def run(c):
    return asyncio.run(c)


class _Store:
    def __init__(self, rows=None):
        self.rows = rows or {}
        self.puts = []

    async def get(self, collection, key):
        return self.rows.get(key)

    async def put(self, collection, key, value):
        self.puts.append(key)
        self.rows[key] = value


def _mk(store):
    return TenantErasure(store=store, pii_salt=_SALT)


def test_a_branding_contact_matching_the_subject_is_cleared():
    # branding is a free-form dict documented as holding email_sender — which can be a named
    # individual's address rather than a role account.
    store = _Store({"acme": {"id": "acme", "name": "Acme Ltd",
                             "branding": {"email_sender": _SUB, "logo": "logo.png"}}})
    out = run(_mk(store).erase("acme", _SUB))
    assert store.rows["acme"]["branding"]["email_sender"] == ""
    assert store.rows["acme"]["branding"]["logo"] == "logo.png"   # identity is not the person
    assert out["erased"] == ["tenant_branding_contact:1"]


def test_the_company_name_is_never_treated_as_a_person():
    # A tenant NAME is an organisation. Erasing it would delete the customer, not the person.
    store = _Store({"acme": {"id": "acme", "name": "Acme Ltd", "branding": {}}})
    run(_mk(store).erase("acme", _SUB))
    assert store.rows["acme"]["name"] == "Acme Ltd"
    assert store.puts == []


def test_a_role_account_that_is_not_the_subject_survives():
    store = _Store({"acme": {"id": "acme", "branding": {"email_sender": "noreply@acme.com"}}})
    out = run(_mk(store).erase("acme", _SUB))
    assert store.rows["acme"]["branding"]["email_sender"] == "noreply@acme.com"
    assert out["already_absent"] is True


def test_a_missing_tenant_acks_rather_than_failing():
    out = run(_mk(_Store()).erase("gone", _SUB))
    assert out["already_absent"] is True and out["erased"] == []


def test_erasing_twice_is_a_no_op_that_still_acks():
    store = _Store({"acme": {"id": "acme", "branding": {"email_sender": _SUB}}})
    first = run(_mk(store).erase("acme", _SUB))
    second = run(_mk(store).erase("acme", _SUB))
    assert first["erased"] and second["erased"] == []


def test_the_ack_says_where_the_membership_actually_lives():
    # Otherwise "nothing here" reads as "we did not look".
    out = run(_mk(_Store()).erase("acme", _SUB))
    assert "identity-service" in out["note"]
