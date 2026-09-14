"""Security hardening for ports & adapters."""
from __future__ import annotations

import datetime as dt

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.adapters.object_store.keys import validate_key
from app.adapters.token_verifier.hs256 import HS256Verifier
from app.adapters.token_verifier.null import NullVerifier
from app.adapters.token_verifier.rs256 import RS256Verifier
from app.config import Settings
from app.container import build_container
from app.ports.errors import ConfigError, InvalidToken, PortError

HS = "s" * 40


def _settings(**over) -> Settings:
    base = dict(INTERNAL_SERVICE_TOKEN="t" * 40)
    base.update(over)
    return Settings(_env_file=None, **base)


# ── Registry fail-fast (config-injection defense) ───────────────────────────
@pytest.mark.parametrize(
    "kw",
    [
        {"DATASTORE_DRIVER": "bogus"},
        {"CACHE_DRIVER": "bogus"},
        {"TOKEN_VERIFIER_DRIVER": "hax"},
        {"OBJECTSTORE_DRIVER": "ftp"},
    ],
)
def test_unknown_driver_fails_fast(kw):
    with pytest.raises(ConfigError):
        build_container(_settings(**kw))


def test_known_drivers_build():
    c = build_container(_settings())
    assert c.datastore.driver == "memory"
    assert c.token_verifier.driver == "null"


def test_a_provider_missing_its_credential_boots_degraded_not_crashlooped():
    """A KNOWN provider whose credential is not set yet (Meta WhatsApp on a fresh install) must not
    crash the whole service at boot — it is dropped and the channel degrades to console, so the
    process comes up and every other channel/route works while the token is still absent."""
    c = build_container(_settings(WHATSAPP_CHAIN="meta,console"))   # no WHATSAPP_ACCESS_TOKEN set
    assert c.whatsapp is not None                                   # the service built, no ConfigError
    # meta was dropped (unconfigured); console remains as the floor
    assert "console" in c.whatsapp.driver and "meta" not in c.whatsapp.driver


def test_a_missing_credential_with_no_console_still_falls_back_to_console():
    c = build_container(_settings(WHATSAPP_CHAIN="meta"))           # meta only, unconfigured
    assert c.whatsapp is not None and "console" in c.whatsapp.driver


def test_an_unknown_channel_provider_is_still_fatal():
    # A TYPO in the chain is a config error, not a not-yet-set credential — must still fail fast.
    with pytest.raises(ConfigError):
        build_container(_settings(WHATSAPP_CHAIN="metaa"))


# ── Lazy infra dependency -> friendly ConfigError, not ImportError ──────────
def test_infra_driver_without_extra_is_friendly(monkeypatch):
    """Selecting an infra driver whose extra is missing must raise a friendly ConfigError.

    The absence is SIMULATED rather than assumed: `asyncpg` is installed whenever the
    postgres contract tests run (DATASTORE_TEST_DSN), and a test whose premise silently
    becomes false stops testing anything.
    """
    import sys

    monkeypatch.setitem(sys.modules, "asyncpg", None)   # import_module -> ImportError
    with pytest.raises(ConfigError):
        build_container(_settings(DATASTORE_DRIVER="postgres", DATASTORE_DSN="postgresql://x/y"))


# ── Deny-by-default token verifier ─────────────────────────────────────────
def test_null_verifier_denies_all():
    with pytest.raises(InvalidToken):
        NullVerifier().verify("anything")


# ── Algorithm-confusion defenses (CWE-347) ─────────────────────────────────
def _claims(**over):
    now = dt.datetime.now(dt.timezone.utc)
    base = {"sub": "u", "exp": now + dt.timedelta(minutes=5), "iat": now}
    base.update(over)
    return base


def test_hs256_rejects_alg_none():
    token = jwt.encode(_claims(), key=None, algorithm="none")
    with pytest.raises(InvalidToken):
        HS256Verifier(HS).verify(token)


def test_rs256_rejects_wrong_algorithm_token():
    # RS/HS confusion defense: an RS256-pinned verifier must reject an HS256 token.
    # (Note: PyJWT ALSO refuses at encode-time to use a public key as an HMAC secret,
    #  so the classic forge can't even be crafted — defense in depth.)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    hs_token = jwt.encode(_claims(), "h" * 40, algorithm="HS256")
    with pytest.raises(InvalidToken):
        RS256Verifier(pub).verify(hs_token)


# ── Object-key path traversal (CWE-22) ─────────────────────────────────────
@pytest.mark.parametrize(
    "bad",
    [
        "../etc/passwd",
        "a/../../b",
        "/absolute/path",
        "~/secret",
        "a\\b",
        "a\x00b",
        "..",
        "",
    ],
)
def test_object_key_rejects_traversal(bad):
    with pytest.raises(PortError):
        validate_key(bad)


@pytest.mark.parametrize("ok", ["file.txt", "dir/file.txt", "a/b/c/d.bin", "x_1-2.dat"])
def test_object_key_accepts_safe(ok):
    assert validate_key(ok)
