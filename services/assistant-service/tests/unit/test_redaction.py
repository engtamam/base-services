"""Redaction: what must not leave, and what must not be stored."""
from __future__ import annotations

import time

import pytest

from app.core.redaction import redact


@pytest.mark.parametrize("text,marker", [
    ("my card is 4111 1111 1111 1111", "[card]"),
    ("card 4111-1111-1111-1111 declined", "[card]"),
    ("iban SA0380000000608010167519 please", "[iban]"),
    ("write to ahmed@example.com", "[email]"),
    ("call +966 50 123 4567", "[phone]"),
    ("token sk-abcdefghijklmnop0123", "[secret]"),
])
def test_identifiers_people_type_into_help_boxes_are_removed(text, marker):
    out = redact(text)
    assert marker in out.text and out.changed


def test_the_original_value_is_gone_not_merely_flagged():
    out = redact("my card is 4111 1111 1111 1111")
    assert "4111" not in out.text


def test_ordinary_text_is_untouched():
    out = redact("how do I add a camera to my account?")
    assert not out.changed and out.text == "how do I add a camera to my account?"


def test_which_kinds_matched_are_reported():
    out = redact("mail a@b.co and card 4111 1111 1111 1111")
    assert set(out.hits) == {"email", "card"}


def test_redaction_can_be_switched_off_explicitly():
    assert redact("a@b.co", enabled=False).text == "a@b.co"


def test_redaction_is_on_by_default():
    # The deployment that most needs this is the one that never read the setting.
    assert "[email]" in redact("a@b.co").text


def test_a_long_digit_paste_does_not_hang_the_worker():
    """A pattern with nested quantifiers over an unbounded run backtracks catastrophically on
    exactly this input, and a user can paste it. A redactor that hangs is a denial of service."""
    start = time.monotonic()
    redact("1" * 6000 + "x")
    assert time.monotonic() - start < 2.0


def test_oversized_input_is_truncated_rather_than_passed_through_unscanned():
    # Passing the tail through would mean the UNSCANNED portion is the part that leaks.
    out = redact("a@b.co " + "z" * 100 + " c@d.co", max_chars=20)
    assert "c@d.co" not in out.text


def test_redaction_is_one_way():
    # No vault, no re-inflation: reversible redaction is a second copy of the secret plus a key.
    out = redact("card 4111 1111 1111 1111")
    assert "4111" not in out.text and "[card]" in out.text
