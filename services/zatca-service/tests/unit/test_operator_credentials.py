"""The signer credential, changeable by an operator after launch.

ZATCA credentials are issued by the authority to the taxpayer and replaced on the authority's
schedule, not ours. The tests that matter are that a stored one actually reaches the signer, that
it can be changed without a redeploy, and that a broken one degrades to the configured value rather
than stopping every clearance — for a tax integration an outage is a filing deadline.
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.operator_credentials import SignerCredentials
from app.core.secret_store import SecretStore

KEK = "unit-test-kek"


def run(c):
    return asyncio.run(c)


class _Settings:
    ZATCA_SIGNER_TOKEN = "env-token"


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _store():
    return SecretStore(MemoryDataStore(), KEK, now=lambda: 0.0)


def test_with_nothing_stored_the_configured_token_is_used():
    """A deployment that has not migrated behaves exactly as it does today."""
    assert run(SignerCredentials(_Settings(), _store()).token("")) == "env-token"


def test_without_a_secret_store_the_configured_token_is_used():
    assert run(SignerCredentials(_Settings(), None).token("")) == "env-token"


def test_a_stored_token_beats_the_configured_one():
    s = _store()
    run(s.set(tenant="", name="ZATCA_SIGNER_TOKEN", value="operator-set"))
    assert run(SignerCredentials(_Settings(), s).token("")) == "operator-set"


def test_a_taxpayer_can_hold_its_own_signer_credential():
    s = _store()
    run(s.set(tenant="acme", name="ZATCA_SIGNER_TOKEN", value="acme-token"))
    c = SignerCredentials(_Settings(), s)
    assert run(c.token("acme")) == "acme-token"
    assert run(c.token("globex")) == "env-token"


def test_a_rotation_reaches_the_clearance_path_after_the_window():
    """If the cache were a permanent capture the operator's change would never take effect."""
    s, clock = _store(), _Clock()
    c = SignerCredentials(_Settings(), s, ttl_s=30, now=clock)
    run(c.token(""))
    run(s.set(tenant="", name="ZATCA_SIGNER_TOKEN", value="rotated"))
    clock.t = 31.0
    assert run(c.token("")) == "rotated"


def test_within_the_window_the_credential_is_not_read_again():
    s, clock = _store(), _Clock()
    c = SignerCredentials(_Settings(), s, ttl_s=30, now=clock)
    run(c.token(""))
    run(s.set(tenant="", name="ZATCA_SIGNER_TOKEN", value="changed"))
    clock.t = 29.0
    assert run(c.token("")) == "env-token"


def test_invalidating_makes_a_change_visible_immediately():
    s = _store()
    c = SignerCredentials(_Settings(), s, ttl_s=3600)
    run(c.token(""))
    run(s.set(tenant="", name="ZATCA_SIGNER_TOKEN", value="just-set-it"))
    c.invalidate("")
    assert run(c.token("")) == "just-set-it"


def test_an_undecryptable_credential_falls_back_instead_of_stopping_clearance():
    store = MemoryDataStore()
    run(SecretStore(store, KEK, now=lambda: 0.0).set(
        tenant="", name="ZATCA_SIGNER_TOKEN", value="sealed-under-the-old-kek"))
    wrong = SecretStore(store, "a-different-kek", now=lambda: 0.0)
    assert run(SignerCredentials(_Settings(), wrong).token("")) == "env-token"


# ── the seal key is deliberately NOT operator-settable ────────────────────────
def test_the_key_that_seals_stored_certificates_cannot_be_set_at_runtime():
    """ZATCA_SEAL_KEY protects every stored CSID. A key settable from the panel protects nothing
    from someone who has the panel, and rotating it at runtime would make every stored certificate
    unreadable at once rather than one at a time."""
    from app.config import Settings
    from app.core.runtime_config import settable

    assert not settable("ZATCA_SEAL_KEY", Settings)
    assert not settable("SECRETS_KEK", Settings)
