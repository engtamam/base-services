"""Conversation storage: scoped, pseudonymous, encrypted, erasable."""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.conversations import ConversationStore, MessageCrypto
from app.ports.errors import ConfigError

SALT = "s" * 32
KEY = "k" * 40


def run(c):
    return asyncio.run(c)


def _store(crypto=None):
    n = {"i": 0}
    def new_id():
        n["i"] += 1
        return f"id{n['i']}"
    return ConversationStore(MemoryDataStore(), pii_salt=SALT,
                             crypto=crypto or MessageCrypto(KEY, required=True),
                             now=lambda: "2026-01-01T00:00:00Z", new_id=new_id)


def _seed(cs, tenant="acme", subject="u@x.com"):
    conv = run(cs.start(tenant=tenant, subject=subject, title="T"))
    run(cs.add(conversation_id=conv.id, tenant=tenant, subject=subject,
               role="user", content="my card is 4111 1111 1111 1111"))
    return conv


# ── scoping ───────────────────────────────────────────────────────────────────
def test_another_tenant_cannot_read_the_conversation():
    cs = _store(); conv = _seed(cs)
    assert run(cs.get(conversation_id=conv.id, tenant="globex", subject="u@x.com")) is None


def test_another_subject_in_the_same_tenant_cannot_read_it():
    cs = _store(); conv = _seed(cs)
    assert run(cs.get(conversation_id=conv.id, tenant="acme", subject="other@x.com")) is None


def test_a_conversation_id_is_not_a_bearer_token():
    # Knowing the id must not be enough; both tenant and subject are re-checked on every read.
    cs = _store(); conv = _seed(cs)
    msgs, _ = run(cs.messages(conversation_id=conv.id, tenant="globex", subject="u@x.com"))
    assert msgs == []


def test_appending_to_someone_elses_conversation_is_refused():
    cs = _store(); conv = _seed(cs)
    with pytest.raises(PermissionError):
        run(cs.add(conversation_id=conv.id, tenant="acme", subject="mallory@x.com",
                   role="user", content="hello"))


def test_the_owner_reads_their_own():
    cs = _store(); conv = _seed(cs)
    assert run(cs.get(conversation_id=conv.id, tenant="acme", subject="u@x.com")) is not None


# ── pseudonymity + encryption ─────────────────────────────────────────────────
def test_rows_carry_a_pseudonym_not_the_identity():
    cs = _store(); _seed(cs)
    blob = repr(cs._s._data if hasattr(cs._s, "_data") else cs._s.__dict__)
    assert "u@x.com" not in blob


def test_message_content_is_encrypted_at_rest():
    cs = _store(); _seed(cs)
    blob = repr(cs._s._data if hasattr(cs._s, "_data") else cs._s.__dict__)
    assert "4111" not in blob, "card number readable in the store"


def test_content_round_trips_for_the_owner():
    cs = _store(); conv = _seed(cs)
    msgs, _ = run(cs.messages(conversation_id=conv.id, tenant="acme", subject="u@x.com"))
    assert msgs[0].content == "my card is 4111 1111 1111 1111"


def test_encryption_on_without_a_key_refuses_rather_than_storing_plaintext():
    # Plaintext-while-reporting-success is the failure that looks fine for a year.
    with pytest.raises(ConfigError):
        MessageCrypto("", required=True)


def test_encrypted_history_without_a_key_raises_instead_of_emptying_itself():
    cs = _store(); conv = _seed(cs)
    cs._crypto = MessageCrypto("", required=False)
    with pytest.raises(ConfigError):
        run(cs.messages(conversation_id=conv.id, tenant="acme", subject="u@x.com"))


def test_plaintext_written_before_encryption_still_reads():
    cs = _store(MessageCrypto("", required=False))
    conv = _seed(cs)
    msgs, _ = run(cs.messages(conversation_id=conv.id, tenant="acme", subject="u@x.com"))
    assert msgs[0].content.startswith("my card")


# ── erasure ───────────────────────────────────────────────────────────────────
def test_erasing_a_subject_removes_conversations_and_messages():
    cs = _store(); conv = _seed(cs)
    out = run(cs.erase_subject(tenant="acme", subject="u@x.com"))
    assert out == {"conversations": 1, "messages": 1}
    assert run(cs.get(conversation_id=conv.id, tenant="acme", subject="u@x.com")) is None


def test_erasure_leaves_another_subject_untouched():
    cs = _store()
    mine = _seed(cs, subject="u@x.com")
    theirs = _seed(cs, subject="other@x.com")
    run(cs.erase_subject(tenant="acme", subject="u@x.com"))
    assert run(cs.get(conversation_id=theirs.id, tenant="acme", subject="other@x.com")) is not None
    assert run(cs.get(conversation_id=mine.id, tenant="acme", subject="u@x.com")) is None


def test_erasure_leaves_another_tenant_untouched():
    cs = _store()
    a = _seed(cs, tenant="acme"); b = _seed(cs, tenant="globex")
    run(cs.erase_subject(tenant="acme", subject="u@x.com"))
    assert run(cs.get(conversation_id=b.id, tenant="globex", subject="u@x.com")) is not None


def test_erasing_twice_is_a_no_op():
    cs = _store(); _seed(cs)
    run(cs.erase_subject(tenant="acme", subject="u@x.com"))
    assert run(cs.erase_subject(tenant="acme", subject="u@x.com")) == {"conversations": 0, "messages": 0}


def test_delete_refuses_for_a_non_owner():
    cs = _store(); conv = _seed(cs)
    assert run(cs.delete(conversation_id=conv.id, tenant="acme", subject="mallory@x.com")) is False
    assert run(cs.get(conversation_id=conv.id, tenant="acme", subject="u@x.com")) is not None


# ── listing ───────────────────────────────────────────────────────────────────
def test_listing_is_scoped_to_the_caller():
    cs = _store()
    _seed(cs, subject="u@x.com"); _seed(cs, subject="other@x.com")
    mine, _ = run(cs.list(tenant="acme", subject="u@x.com"))
    assert len(mine) == 1


# ── retention, actually enforced ──────────────────────────────────────────────
def test_retention_deletes_what_is_past_the_cutoff():
    """A policy that is stored, validated and surfaced but never executed is the most common
    compliance defect there is: every review passes and the data is still there years later."""
    cs = _store()
    conv = _seed(cs)
    out = run(cs.enforce_retention(cutoff_iso="2030-01-01T00:00:00Z"))
    assert out == {"conversations": 1, "messages": 1}
    assert run(cs.get(conversation_id=conv.id, tenant="acme", subject="u@x.com")) is None


def test_retention_keeps_what_is_inside_the_window():
    cs = _store(); conv = _seed(cs)
    out = run(cs.enforce_retention(cutoff_iso="2020-01-01T00:00:00Z"))
    assert out == {"conversations": 0, "messages": 0}
    assert run(cs.get(conversation_id=conv.id, tenant="acme", subject="u@x.com")) is not None


def test_retention_removes_messages_with_their_conversation():
    # A stranded message is content nobody can find to erase.
    cs = _store(); conv = _seed(cs)
    run(cs.enforce_retention(cutoff_iso="2030-01-01T00:00:00Z"))
    msgs, _ = run(cs.messages(conversation_id=conv.id, tenant="acme", subject="u@x.com"))
    assert msgs == []


def test_retention_purges_all_expired_across_pages():
    # Retention must PAGE THROUGH THE WHOLE collection, not stop at the first key-ordered prefix —
    # a small page size (limit) is the batch size, not a coverage cap. The old single-pass left every
    # conversation beyond the prefix un-purged (PII kept past policy). With 5 expired rows and a page
    # of 2, all 5 are still purged over the paged loop.
    cs = _store()
    for _ in range(5):
        _seed(cs)
    out = run(cs.enforce_retention(cutoff_iso="2030-01-01T00:00:00Z", limit=2))
    assert out["conversations"] == 5
