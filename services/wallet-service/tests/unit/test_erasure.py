"""Erasure — wallet. It holds no person, so the test's job is to keep that true.

The valuable test here is not "did it delete?" but the guard at the bottom: if someone later adds a
person-linked field to a wallet model, that test fails and forces the erasure decision to be made
deliberately instead of accumulating PII in silence.
"""
from __future__ import annotations

import asyncio
import dataclasses

from app.core import models
from app.core.erasure import FINANCIAL_BASIS, WalletErasure

_SALT = "s" * 32


def run(c):
    return asyncio.run(c)


def test_answers_already_absent_with_a_pseudonym():
    out = run(WalletErasure(pii_salt=_SALT).erase("acme", "u@example.com"))
    assert out["already_absent"] is True
    assert out["erased"] == [] and out["retained"] == []
    assert out["subject_ref"].startswith("sub_")
    assert "u@example.com" not in out["subject_ref"]


def test_the_answer_explains_why_there_is_nothing_here():
    # "Nothing to erase" without a reason is indistinguishable from "we did not look".
    out = run(WalletErasure(pii_salt=_SALT).erase("acme", "u@example.com"))
    assert "keyed by tenant" in out["note"]
    assert FINANCIAL_BASIS in out["note"]


def test_it_touches_no_storage_at_all():
    class _Exploding:
        async def find(self, *a, **k):
            raise AssertionError("erasure must not read: there is nothing here to look for")

        async def delete(self, *a, **k):
            raise AssertionError("erasure must never delete a financial record")

    run(WalletErasure(store=_Exploding(), pii_salt=_SALT).erase("acme", "u@example.com"))


def test_no_wallet_model_has_a_person_linked_field():
    """The guard that gives this file its value.

    Wallet's erasure is a no-op ONLY because money here belongs to a tenant, never to a person. If
    a field like user_id or email appears on one of these models, that stops being true and this
    endpoint starts lying. Failing here forces the decision — retain-and-pseudonymise like billing,
    or delete — to be made in review rather than by omission.
    """
    person_fields = {"subject", "subject_ref", "user", "user_id", "email", "phone", "name",
                     "customer", "customer_id", "actor", "ip"}
    for name in ("Wallet", "LedgerEntry", "Hold", "Grant"):
        model = getattr(models, name, None)
        if model is None or not dataclasses.is_dataclass(model):
            continue
        found = {f.name for f in dataclasses.fields(model)} & person_fields
        assert not found, (
            f"{name} gained person-linked field(s) {found}. wallet-service now holds subject data "
            f"and its erasure is no longer a no-op — decide explicitly in app/core/erasure.py.")
