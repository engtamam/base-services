"""Per-account lockout + refresh-token reuse detection (pass-2 hardening)."""
from __future__ import annotations

import asyncio
import pathlib
import re

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.login_identifier import UsernameIdentifier
from app.core.auth_service import AuthService
from app.core.login_attempts import LoginAttemptStore
from app.core.refresh_store import RefreshStore
from app.core.user_repository import UserRepository


def run(c):
    return asyncio.run(c)


class Hasher:
    driver = "plain"

    def hash(self, pw):
        return "h:" + pw

    def verify(self, pw, h):
        return h == "h:" + pw


class Issuer:
    driver = "i"

    def issue_access(self, sub, claims, ttl):
        return f"access.{sub}"

    def issue_refresh(self, sub, jti, ttl):
        return f"refresh.{sub}.{jti}"


class Verifier:
    driver = "v"

    def __init__(self):
        self.tokens = {}

    def verify(self, token):
        return self.tokens[token]


def _svc(st, verifier=None, max_attempts=3):
    lockout = LoginAttemptStore(st, max_attempts=max_attempts, window_seconds=900, lockout_seconds=900)
    return AuthService(users=UserRepository(st, UsernameIdentifier()), refresh=RefreshStore(st), hasher=Hasher(),
                       issuer=Issuer(), verifier=verifier or Verifier(), mfa=None, identifier=UsernameIdentifier(),
                       access_ttl=900, refresh_ttl=3600, min_password_len=4, lockout=lockout)


# ── account lockout ───────────────────────────────────────────────────────────
def test_account_locks_after_threshold():
    st = MemoryDataStore()
    svc = _svc(st, max_attempts=3)
    run(svc.register("t1", "alice", "correct-horse"))
    for _ in range(3):
        assert run(svc.authenticate("t1", "alice", "wrong")) is None
    # now even the CORRECT password is refused — the account is locked
    assert run(svc.authenticate("t1", "alice", "correct-horse")) is None


def test_successful_login_resets_the_counter():
    st = MemoryDataStore()
    svc = _svc(st, max_attempts=3)
    run(svc.register("t1", "alice", "correct-horse"))
    assert run(svc.authenticate("t1", "alice", "wrong")) is None
    assert run(svc.authenticate("t1", "alice", "wrong")) is None
    assert run(svc.authenticate("t1", "alice", "correct-horse")) is not None   # resets
    for _ in range(2):
        run(svc.authenticate("t1", "alice", "wrong"))
    assert run(svc.authenticate("t1", "alice", "correct-horse")) is not None   # not locked


def test_lockout_does_not_reveal_account_existence():
    st = MemoryDataStore()
    svc = _svc(st, max_attempts=2)
    # hammering a NON-existent user is accepted the same way (generic None), and the store
    # tracks it without ever confirming the account is real
    assert run(svc.authenticate("t1", "ghost", "x")) is None
    assert run(svc.authenticate("t1", "ghost", "x")) is None
    assert run(svc.authenticate("t1", "ghost", "x")) is None


def test_lockout_is_per_account_not_global():
    st = MemoryDataStore()
    svc = _svc(st, max_attempts=2)
    run(svc.register("t1", "alice", "pw-alice"))
    run(svc.register("t1", "bob", "pw-bob"))
    run(svc.authenticate("t1", "alice", "wrong"))
    run(svc.authenticate("t1", "alice", "wrong"))            # alice locked
    assert run(svc.authenticate("t1", "alice", "pw-alice")) is None
    assert run(svc.authenticate("t1", "bob", "pw-bob")) is not None   # bob unaffected


# ── refresh reuse detection ───────────────────────────────────────────────────
def test_reused_refresh_token_revokes_the_whole_family():
    st = MemoryDataStore()
    v = Verifier()
    svc = _svc(st, verifier=v)
    user = run(svc.register("t1", "alice", "pw-alice"))
    t1 = run(svc.issue_tokens(user))
    t2 = run(svc.issue_tokens(user))                         # a second active session
    # wire the verifier to decode the refresh tokens
    for toks in (t1, t2):
        jti = toks["refresh_token"].split(".")[-1]
        v.tokens[toks["refresh_token"]] = {"type": "refresh", "jti": jti, "sub": user.id}
    # legitimately rotate t1 once
    rotated = run(svc.refresh(t1["refresh_token"]))
    assert rotated is not None
    # an attacker REPLAYS the now-rotated t1 → theft signal → nuke the family
    assert run(svc.refresh(t1["refresh_token"])) is None
    # ...and t2 (a different session) is now dead too
    assert run(svc.refresh(t2["refresh_token"])) is None


def test_expired_refresh_is_not_treated_as_theft(monkeypatch):
    # identity-002: a naturally-expired refresh token replayed is a plain invalid refresh (None),
    # NOT theft — it must NOT revoke the family and kill the user's other live sessions.
    from app.core import refresh_store as rs
    st = MemoryDataStore()
    v = Verifier()
    svc = _svc(st, verifier=v)
    user = run(svc.register("t1", "alice", "pw-alice"))
    old = run(svc.issue_tokens(user))       # will be force-expired
    live = run(svc.issue_tokens(user))      # a separate, still-valid session
    for toks in (old, live):
        jti = toks["refresh_token"].split(".")[-1]
        v.tokens[toks["refresh_token"]] = {"type": "refresh", "jti": jti, "sub": user.id}
    # force `old`'s stored row to be expired (exp in the past), not revoked
    old_jti = old["refresh_token"].split(".")[-1]
    row = run(st.get("refresh_tokens", old_jti))
    row["exp"] = 1.0
    run(st.put("refresh_tokens", old_jti, row))
    assert run(st.get("refresh_tokens", old_jti))  # sanity
    assert run(RefreshStore(st).status(old_jti)) == "expired"
    # replaying the expired token is refused, but does NOT nuke the family
    assert run(svc.refresh(old["refresh_token"])) is None
    assert run(svc.refresh(live["refresh_token"])) is not None   # the live session survives


def test_refresh_reuse_is_audited():
    # identity-004: detected reuse + family revocation emits a witness (audit + telemetry).
    class _Aud:
        def __init__(self): self.calls = []
        async def record(self, action, **k): self.calls.append(action)

    class _Tel:
        def __init__(self): self.counts = []
        def counter(self, name, value=1, **labels): self.counts.append(name)

    st = MemoryDataStore()
    v = Verifier()
    aud, tel = _Aud(), _Tel()
    svc = AuthService(users=UserRepository(st, UsernameIdentifier()), refresh=RefreshStore(st),
                      hasher=Hasher(), issuer=Issuer(), verifier=v, mfa=None,
                      identifier=UsernameIdentifier(), access_ttl=900, refresh_ttl=3600,
                      min_password_len=4, auditor=aud, telemetry=tel)
    user = run(svc.register("t1", "alice", "pw-alice"))
    toks = run(svc.issue_tokens(user))
    jti = toks["refresh_token"].split(".")[-1]
    v.tokens[toks["refresh_token"]] = {"type": "refresh", "jti": jti, "sub": user.id}
    run(svc.refresh(toks["refresh_token"]))                 # legit rotation
    assert run(svc.refresh(toks["refresh_token"])) is None  # replay → theft
    assert "auth.refresh_reuse_detected" in aud.calls
    assert "auth.refresh_reuse_detected" in tel.counts


def test_normal_rotation_still_works():
    st = MemoryDataStore()
    v = Verifier()
    svc = _svc(st, verifier=v)
    user = run(svc.register("t1", "alice", "pw-alice"))
    toks = run(svc.issue_tokens(user))
    jti = toks["refresh_token"].split(".")[-1]
    v.tokens[toks["refresh_token"]] = {"type": "refresh", "jti": jti, "sub": user.id}
    assert run(svc.refresh(toks["refresh_token"])) is not None


def test_every_settings_field_is_consumed_somewhere():
    from app.config import Settings

    root = pathlib.Path(__file__).resolve().parents[2]
    files = [p for p in root.rglob("*") if p.is_file() and p.suffix in (".py", ".toml", "")
             and "tests" not in p.parts and ".venv" not in p.parts and "__pycache__" not in p.parts]
    lines: list[str] = []
    for p in files:
        try:
            lines.extend(p.read_text(encoding="utf-8").splitlines())
        except (UnicodeDecodeError, OSError):
            continue
    unused = []
    for name in Settings.model_fields:
        decl = re.compile(rf"^\s*{re.escape(name)}\s*:")
        if not [ln for ln in lines if re.search(rf"\b{re.escape(name)}\b", ln) and not decl.match(ln)]:
            unused.append(name)
    assert not unused, f"config knobs declared but never consumed: {unused}"
