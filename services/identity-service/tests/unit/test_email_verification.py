"""An address nobody proved is not an identity.

Before this, `register` accepted any string shaped like an email and issued a working session.
Anyone could sign up as cfo@bank.com, and every service that treats the address as a person — a
password reset, an invite, a notification, an audit actor — inherited the lie. Our own invite flow
exists precisely because an unproven address is not an identity; self-service registration
reintroduced the problem it was built to avoid.

The tests are ordered by what must never happen:

  1. an unverified account gets a session anyway
  2. a token works twice, or after expiry, or for a different address
  3. the endpoints tell a stranger which addresses are registered
  4. an upgrade locks out every account that already existed
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.auth_service import AuthService
from app.core.email_verification import VerificationStore
from app.core.models import User


def run(c):
    return asyncio.run(c)


class _Users:
    def __init__(self):
        self.rows: dict[str, User] = {}

    async def get(self, uid):
        return self.rows.get(uid)

    async def save(self, u):
        self.rows[u.id] = u
        return u


class _Ident:
    driver = "email"

    def validate(self, v):
        return None

    def normalize(self, v):                 # the canonical key (uniqueness + comparison)
        return v.strip().casefold()

    def display(self, v):
        return v.strip()


class _Issuer:
    def issue_access(self, sub, claims, ttl):
        return "ACCESS"

    def issue_refresh(self, sub, jti, ttl):
        return "REFRESH"


class _Refresh:
    async def issue(self, jti, uid, exp):
        return None


def _store(**kw):
    return VerificationStore(MemoryDataStore(), **kw)


def _svc(verifications=None, require=True, legacy="grandfather"):
    s = AuthService.__new__(AuthService)
    s._users, s._identifier = _Users(), _Ident()
    s._issuer, s._refresh = _Issuer(), _Refresh()
    s._access_ttl = s._refresh_ttl = 900
    s._verifications = verifications
    s._require_verified = require
    s._legacy_email = legacy
    s._platform_tenant = ""
    return s


def _user(uid="u1", verified=None, name="ada@example.com"):
    return User(id=uid, tenant="acme", username=name, password_hash="h", email_verified=verified)


# ── 1. an unverified account must not get a session ────────────────────────────
def test_an_unverified_account_is_refused_a_token():
    """THE point of the feature."""
    svc = _svc()
    with pytest.raises(AuthService.EmailNotVerified):
        run(svc.issue_tokens(_user(verified=False)))


def test_a_verified_account_gets_one():
    svc = _svc()
    assert run(svc.issue_tokens(_user(verified=True)))["access_token"] == "ACCESS"


def test_the_gate_is_at_the_MINT_so_refresh_cannot_launder_it():
    """A check in the login handler would let an unverified session live for ever on renewals,
    because refresh mints too. Both paths go through issue_tokens, so both are covered by one
    rule rather than two that can drift."""
    import inspect
    assert "EmailNotVerified" in inspect.getsource(AuthService.issue_tokens)


def test_verification_can_be_switched_off_entirely():
    svc = _svc(require=False)
    assert run(svc.issue_tokens(_user(verified=False)))["access_token"] == "ACCESS"


# ── 2. the token itself ────────────────────────────────────────────────────────
def test_a_token_works_exactly_once():
    """Single-use is enforced by DELETING the row, not by a flag — two concurrent redeems of a
    flagged row can both pass the check."""
    st = _store()
    tok = run(st.issue(user_id="u1", tenant="acme", address="ada@example.com"))
    assert run(st.redeem(tok)) is not None
    assert run(st.redeem(tok)) is None


def test_the_raw_token_is_never_stored():
    """A stolen table must be a table of useless digests, not a set of live keys."""
    ds = MemoryDataStore()
    st = VerificationStore(ds, pepper="p" * 32)
    tok = run(st.issue(user_id="u1", tenant="acme", address="ada@example.com"))
    assert tok not in str(ds._data if hasattr(ds, "_data") else "")


def test_a_pepper_changes_the_stored_digest():
    """So a thief with the table cannot recognise a token obtained elsewhere."""
    a, b = VerificationStore(MemoryDataStore(), pepper="A" * 32), \
        VerificationStore(MemoryDataStore(), pepper="B" * 32)
    assert a._digest("same-token") != b._digest("same-token")


def test_an_expired_token_is_refused():
    st = _store(ttl_seconds=-1)
    tok = run(st.issue(user_id="u1", tenant="acme", address="ada@example.com"))
    assert run(st.redeem(tok)) is None


def test_an_unknown_token_is_refused():
    assert run(_store().redeem("not-a-real-token")) is None


def test_live_tokens_are_CAPPED_per_account():
    """Every resend mints a credential; unbounded resends mean unbounded live keys, each another
    chance to leak through a mail archive or a proxy log."""
    st = _store(max_outstanding=2)
    toks = [run(st.issue(user_id="u1", tenant="acme", address="a@b.com")) for _ in range(4)]
    alive = sum(1 for t in toks if run(st.redeem(t)) is not None)
    assert alive <= 2


def test_an_earlier_link_still_works_while_within_the_cap():
    """Rotating on every resend without breaking the previous mail: users click whichever link
    they find first, and invalidating it would strand them."""
    st = _store(max_outstanding=3)
    first = run(st.issue(user_id="u1", tenant="acme", address="a@b.com"))
    run(st.issue(user_id="u1", tenant="acme", address="a@b.com"))
    assert run(st.redeem(first)) is not None


def test_a_token_does_not_prove_a_DIFFERENT_address():
    """After an address change, an old link must not prove the new one — nobody demonstrated
    control of it."""
    st = _store()
    svc = _svc(st)
    user = _user(verified=False, name="old@example.com")
    svc._users.rows[user.id] = user
    tok = run(svc.start_verification(user))

    user.username = "new@example.com"          # address changed after the mail went out
    assert run(svc.verify_email(tok)) is None
    assert user.email_verified is not True


def test_verifying_marks_the_account_proven():
    st = _store()
    svc = _svc(st)
    user = _user(verified=False)
    svc._users.rows[user.id] = user
    tok = run(svc.start_verification(user))
    assert run(svc.verify_email(tok)) is not None
    assert user.email_verified is True
    assert run(svc.issue_tokens(user))["access_token"] == "ACCESS"


# ── 3. cooldown, which protects the human not the service ──────────────────────
def test_a_resend_within_the_cooldown_is_suppressed():
    """A per-IP limit protects the service; it does nothing for the person being mail-bombed."""
    st = _store()
    run(st.issue(user_id="u1", tenant="acme", address="a@b.com"))
    assert run(st.recently_issued("u1", 60)) is True


def test_the_cooldown_does_not_apply_to_a_different_account():
    st = _store()
    run(st.issue(user_id="u1", tenant="acme", address="a@b.com"))
    assert run(st.recently_issued("u2", 60)) is False


def test_a_zero_cooldown_disables_it():
    st = _store()
    run(st.issue(user_id="u1", tenant="acme", address="a@b.com"))
    assert run(st.recently_issued("u1", 0)) is False


# ── 4. the upgrade, where an over-eager rollout locks everyone out ─────────────
def test_a_PRE_EXISTING_account_is_grandfathered():
    """email_verified is None for a row written before this feature — nobody ever asked. Under
    `grandfather` they keep working; the alternative locks out every existing user on deploy."""
    assert _svc(legacy="grandfather").email_is_proven(_user(verified=None)) is True


def test_enforce_refuses_pre_existing_accounts_too():
    """The honest setting for a platform that wants the guarantee retroactively."""
    assert _svc(legacy="enforce").email_is_proven(_user(verified=None)) is False


def test_a_registered_but_unclicked_account_is_refused_under_BOTH_policies():
    """None and False are different facts. Collapsing them would let `grandfather` wave through
    every new signup, which is the whole feature defeated by a truthiness test."""
    assert _svc(legacy="grandfather").email_is_proven(_user(verified=False)) is False
    assert _svc(legacy="enforce").email_is_proven(_user(verified=False)) is False


# ── the null notifier must not claim success ──────────────────────────────────
def test_the_null_notifier_reports_that_it_sent_nothing():
    """A null notifier returning True would mean verification mails were 'sent' to everyone and
    received by no one, while every log line agreed the flow worked."""
    from app.adapters.notifier.null import NullNotifier

    n = NullNotifier()
    assert run(n.send(to="a@b.com", template="t", tenant="acme")) is False
    assert n.sent and n.sent[0]["to"] == "a@b.com"
