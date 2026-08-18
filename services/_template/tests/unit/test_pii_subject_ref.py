"""subject_ref is a pseudonym that must never be double-wrapped.

The failure this guards: an erasure re-hashes an already-hashed subject_ref, matches zero rows, reports
already_absent, and the request closes COMPLETED over data it never touched — a false "erased", the
worst answer a privacy system can give.
"""
from __future__ import annotations

from app.core import pii


def test_subject_ref_is_stable_tenant_scoped_and_salted():
    a = pii.subject_ref("salt", "t1", "user@example.com")
    assert a == pii.subject_ref("salt", "t1", "user@example.com")     # stable
    assert a != pii.subject_ref("salt", "t2", "user@example.com")     # tenant-scoped
    assert a != pii.subject_ref("other", "t1", "user@example.com")    # salted
    assert pii.is_subject_ref(a)


def test_subject_ref_never_double_wraps():
    once = pii.subject_ref("salt", "t1", "user@example.com")
    assert pii.subject_ref("salt", "t1", once) == once               # idempotent
    assert pii.subject_ref("salt", "t2", once) == once               # even under a different tenant


def test_an_empty_salt_or_subject_is_refused():
    import pytest
    with pytest.raises(ValueError):
        pii.subject_ref("", "t1", "u")
    with pytest.raises(ValueError):
        pii.subject_ref("salt", "t1", "")
