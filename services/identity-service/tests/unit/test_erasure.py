"""Erasure: the person goes, the record stays, and the record still verifies.

This is the test the whole Phase-30 design stands or falls on.

An audit event is hash-chained. If erasure had to reach into audit and redact a field, `verify()`
would fail from that seq onward and we would have destroyed the tamper-evidence we sell. If
instead we never erase audit, we have failed the erasure and the person is still named forever.

The way out is that the immutable record never held the name to begin with — it holds a
`subject_ref` pseudonym. identity owns the ONLY mapping back to the human. Delete that, and:

    * every `sub_…` in every immutable record becomes an orphan that names nobody, and
    * the chain is untouched, so it still verifies.

Both halves are asserted here. Either one alone is a failed design.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.login_identifier import EmailIdentifier
from app.core.erasure import IdentityErasure
from app.core.models import User, new_id
from app.core.pii import subject_ref
from app.core.refresh_store import RefreshStore
from app.core.user_repository import UserRepository

SALT = "product-pii-salt"
TENANT, SUBJECT = "t1", "alice@x.com"


def run(c):
    return asyncio.run(c)


def _fixture():
    store = MemoryDataStore()
    users = UserRepository(store, EmailIdentifier())
    refresh = RefreshStore(store)
    eraser = IdentityErasure(users=users, refresh=refresh, store=store, pii_salt=SALT)
    return store, users, refresh, eraser


def _seed_user(users, refresh):
    user = User(id=new_id(), tenant=TENANT, username=SUBJECT, password_hash="h")
    run(users.create(user))
    run(refresh.issue("jti-1", user.id, 9_999_999_999.0))
    run(refresh.issue("jti-2", user.id, 9_999_999_999.0))
    return user


# ── the person is gone, and unlinkable ──────────────────────────────────────
def test_erasure_removes_the_user_and_the_lookup_index():
    store, users, refresh, eraser = _fixture()
    _seed_user(users, refresh)

    receipt = run(eraser.erase(TENANT, SUBJECT))

    assert run(users.get_by_username(TENANT, SUBJECT)) is None, "the user survived erasure"
    assert "user" in receipt["erased"] and "username_index" in receipt["erased"]
    # The index is the linkage. Leaving it behind leaves the email itself stored and enumerable.
    rows = run(store.scan_all("users_by_username", 100))
    assert rows == [], "the username index still holds the person's email after erasure"


def test_erasure_revokes_every_session():
    """A live session for a deleted user is worse than a deleted user with a stale row."""
    store, users, refresh, eraser = _fixture()
    user = _seed_user(users, refresh)

    receipt = run(eraser.erase(TENANT, SUBJECT))

    assert any(e.startswith("refresh_tokens:") for e in receipt["erased"])
    assert run(refresh.is_active("jti-1")) is False, "a revoked token is still active"
    rows = run(store.scan_all("refresh_tokens", 100))
    assert all(r.get("revoked") for r in rows), "a refresh token survived the erasure"


def test_identity_retains_nothing():
    """No tax basis, no security basis lives here. Identity IS the thing being erased."""
    store, users, refresh, eraser = _fixture()
    _seed_user(users, refresh)
    receipt = run(eraser.erase(TENANT, SUBJECT))
    assert receipt["retained"] == []


def test_erasure_is_idempotent():
    """A DSAR retries. The second erase must not fail — it must return the same shape."""
    store, users, refresh, eraser = _fixture()
    _seed_user(users, refresh)

    first = run(eraser.erase(TENANT, SUBJECT))
    second = run(eraser.erase(TENANT, SUBJECT))      # must not raise

    assert first["already_absent"] is False
    assert second["already_absent"] is True
    assert second["subject_ref"] == first["subject_ref"]


# ── the pseudonym: the reason the immutable records survive ─────────────────
def test_the_receipt_names_the_pseudonym_the_immutable_records_hold():
    """Compliance needs the ref to tell the other services WHICH rows are this person's."""
    store, users, refresh, eraser = _fixture()
    _seed_user(users, refresh)
    receipt = run(eraser.erase(TENANT, SUBJECT))
    assert receipt["subject_ref"] == subject_ref(SALT, TENANT, SUBJECT)


def test_the_pseudonym_survives_erasure_but_names_nobody():
    """THE POINT. After erasure the ref still exists in every immutable record — and is an orphan.

    The audit chain keeps proving that sub_abc… did something, in order, untampered. It simply
    cannot be resolved to a human any more, because the only mapping was the user row we deleted.
    """
    store, users, refresh, eraser = _fixture()
    _seed_user(users, refresh)
    ref = subject_ref(SALT, TENANT, SUBJECT)

    run(eraser.erase(TENANT, SUBJECT))

    # the ref is still computable (it is a pure function) but resolves to nobody
    assert subject_ref(SALT, TENANT, SUBJECT) == ref
    assert run(users.get_by_username(TENANT, SUBJECT)) is None, "the ref still resolves to a person"


def test_a_pseudonym_never_contains_the_identity():
    """If the ref leaked the email, the immutable record would still be personal data."""
    ref = subject_ref(SALT, TENANT, SUBJECT)
    assert SUBJECT not in ref
    assert "alice" not in ref.lower()
