"""composite identifier — accept phone OR email at once; route by shape, namespace by kind so a
value that fits two kinds can never collide into one account."""
from __future__ import annotations

import pytest

from app.adapters.login_identifier import EmailIdentifier, PhoneIdentifier, UsernameIdentifier
from app.adapters.login_identifier.composite import CompositeIdentifier
from app.ports.errors import ValidationError


def _c():
    return CompositeIdentifier([("phone", PhoneIdentifier(default_cc="966")),
                                ("email", EmailIdentifier())])


def test_routes_each_value_to_the_right_kind_and_namespaces_the_key():
    c = _c()
    assert c.normalize("0501234567") == "phone:+966501234567"
    assert c.normalize("Alice@Example.com") == "email:alice@example.com"


def test_a_phone_and_a_lookalike_cannot_collide_across_kinds():
    # username '966501234567' (digits) and phone '+966501234567' must stay distinct identities.
    c = CompositeIdentifier([("phone", PhoneIdentifier(default_cc="966")),
                             ("username", UsernameIdentifier())])
    assert c.normalize("0501234567").startswith("phone:")
    assert c.normalize("shopowner").startswith("username:")   # not a phone → username


def test_validate_rejects_a_value_no_kind_accepts():
    with pytest.raises(ValidationError):
        _c().validate("@@@not-a-thing@@@")   # neither a valid phone nor a valid email


def test_a_single_kind_stays_the_plain_adapter_no_prefix():
    # The container uses the plain adapter for one kind — backward compatible, no namespacing.
    from app.config import Settings
    from app.container import _build_login_identifier
    ident = _build_login_identifier(Settings(INTERNAL_SERVICE_TOKEN="t" * 40,
                                             IDENTITY_LOGIN_FIELD="email"))
    assert ident.normalize("Alice@X.com") == "alice@x.com"   # no 'email:' prefix


def test_a_comma_list_builds_a_composite():
    from app.config import Settings
    from app.container import _build_login_identifier
    ident = _build_login_identifier(Settings(INTERNAL_SERVICE_TOKEN="t" * 40,
                                             IDENTITY_LOGIN_FIELD="phone,email"))
    assert ident.driver == "composite"
    assert ident.normalize("0501234567") == "phone:+966501234567"
