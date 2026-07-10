"""The login identifier as a port + adapters.

The feature exists because a product wanted email login. The real risk it removes is not "add an
email field" — it is that validation and case-folding become the PORT's job, done once and the
same way for uniqueness and lookup, instead of every product getting them subtly wrong.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.login_identifier import EmailIdentifier, UsernameIdentifier
from app.config import Settings
from app.container import build_container
from app.core.models import User, new_id
from app.core.user_repository import UserExists, UserRepository
from app.ports.errors import ConfigError, ValidationError


def run(c):
    return asyncio.run(c)


# ── the email adapter ───────────────────────────────────────────────────────
@pytest.mark.parametrize("bad", ["not-an-email", "a@b", "a b@x.com", "@x.com", "a@@x.com", ""])
def test_email_adapter_rejects_malformed(bad):
    with pytest.raises(ValidationError):
        EmailIdentifier().validate(bad)


@pytest.mark.parametrize("good", ["a@b.co", "user.name+tag@sub.example.com", "X@Y.io"])
def test_email_adapter_accepts_valid(good):
    EmailIdentifier().validate(good)          # does not raise


def test_email_normalizes_case_and_whitespace():
    e = EmailIdentifier()
    assert e.normalize("  Alice@Example.COM ") == "alice@example.com"
    # display keeps the address the user typed (trimmed), not the folded key
    assert e.display("  Alice@Example.COM ") == "Alice@Example.COM"


# ── uniqueness is on the NORMALIZED key, for BOTH create and lookup ─────────
def _repo(identifier):
    return UserRepository(MemoryDataStore(), identifier)


def test_email_uniqueness_is_case_insensitive():
    repo = _repo(EmailIdentifier())
    run(repo.create(User(id=new_id(), tenant="t1", username="Alice@x.com", password_hash="h")))
    with pytest.raises(UserExists):
        run(repo.create(User(id=new_id(), tenant="t1", username="alice@X.com", password_hash="h")))


def test_email_lookup_finds_a_different_casing():
    repo = _repo(EmailIdentifier())
    run(repo.create(User(id=new_id(), tenant="t1", username="Bob@x.com", password_hash="h")))
    found = run(repo.get_by_username("t1", "BOB@X.COM"))
    assert found is not None and found.username == "Bob@x.com"


def test_username_mode_is_unchanged_behavior():
    """The username adapter's normalize IS the old hardcoded .lower(): default is back-compat."""
    repo = _repo(UsernameIdentifier())
    run(repo.create(User(id=new_id(), tenant="t1", username="Alice", password_hash="h")))
    with pytest.raises(UserExists):
        run(repo.create(User(id=new_id(), tenant="t1", username="alice", password_hash="h")))
    assert run(repo.get_by_username("t1", "ALICE")) is not None


def test_two_tenants_may_share_an_identifier():
    repo = _repo(EmailIdentifier())
    run(repo.create(User(id=new_id(), tenant="t1", username="a@x.com", password_hash="h")))
    run(repo.create(User(id=new_id(), tenant="t2", username="a@x.com", password_hash="h")))  # no raise


# ── config selection: allowlist, fail-fast ──────────────────────────────────
def _settings(**over) -> Settings:
    base = dict(INTERNAL_SERVICE_TOKEN="x" * 32, TOKEN_SIGNING_SECRET="y" * 32)
    base.update(over)
    return Settings(**base)


def test_default_driver_is_username():
    c = build_container(_settings())
    assert c.login_identifier.driver == "username"


def test_email_driver_selectable_by_config():
    c = build_container(_settings(IDENTITY_LOGIN_FIELD="email"))
    assert c.login_identifier.driver == "email"


def test_unknown_login_field_fails_fast_at_boot():
    with pytest.raises(ConfigError):
        build_container(_settings(IDENTITY_LOGIN_FIELD="carrier-pigeon"))


def test_the_login_identifier_appears_in_the_manifest():
    """A knob with no visible consumer is the defect coverage:services exists to catch."""
    c = build_container(_settings(IDENTITY_LOGIN_FIELD="email"))
    ports = {r["port"]: r["driver"] for r in c.describe()}
    assert ports.get("login_identifier") == "email"
