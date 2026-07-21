"""Branding validation — the field nobody thinks of as untrusted."""
from __future__ import annotations

import pytest

from app.core.branding import (Branding, BrandingError, direction_for, parse, resolve)


# ── the injection surface ─────────────────────────────────────────────────────
@pytest.mark.parametrize("bad", [
    "red;} body{display:none}",       # escapes a style rule
    "expression(alert(1))",
    "url(javascript:alert(1))",
    "#12",                            # not a valid hex length
    "rgb(1,2,3)",                     # plausible, still not what we interpolate
])
def test_a_colour_that_is_not_a_hex_colour_is_refused(bad):
    with pytest.raises(BrandingError):
        parse({"primary_color": bad})


@pytest.mark.parametrize("bad", [
    "javascript:alert(1)",
    "data:text/html;base64,PHNjcmlwdD4=",
    "vbscript:msgbox(1)",
    "//evil.example.com/logo.png",     # scheme-relative: inherits the page's scheme
])
def test_a_logo_url_with_a_scheme_we_did_not_choose_is_refused(bad):
    with pytest.raises(BrandingError):
        parse({"logo_url": bad})


def test_markup_in_text_is_refused_rather_than_escaped():
    # Refusing tells the operator; escaping silently mangles their text.
    with pytest.raises(BrandingError):
        parse({"greeting": "Hello <script>alert(1)</script>"})


def test_an_oversized_value_is_refused():
    with pytest.raises(BrandingError):
        parse({"greeting": "x" * 500})


def test_an_arbitrary_position_is_refused():
    # An arbitrary string here would end up inside a style rule.
    with pytest.raises(BrandingError):
        parse({"position": "bottom-right; content:url(x)"})


# ── ordinary values still work ────────────────────────────────────────────────
def test_valid_branding_is_accepted():
    b = parse({"name": "Acme Help", "primary_color": "#0af", "logo_url": "https://a.co/l.png",
               "greeting": "How can we help?", "position": "bottom-left"})
    assert b.name == "Acme Help" and b.primary_color == "#0af"
    assert b.logo_url == "https://a.co/l.png" and b.position == "bottom-left"


@pytest.mark.parametrize("ok", ["#abc", "#aabbcc", "#aabbccdd"])
def test_every_hex_form_is_accepted(ok):
    assert parse({"primary_color": ok}).primary_color == ok


def test_unknown_keys_are_ignored_not_rejected():
    # A newer widget sending a richer object must not break an older service.
    b = parse({"name": "X", "future_field": {"deeply": "nested"}})
    assert b.name == "X" and "future_field" not in b.to_dict()


# ── RTL is a layout fact ──────────────────────────────────────────────────────
@pytest.mark.parametrize("locale,expected", [
    ("ar", "rtl"), ("ar-SA", "rtl"), ("he", "rtl"), ("fa", "rtl"),
    ("en", "ltr"), ("en-GB", "ltr"), ("", "ltr"),
])
def test_direction_follows_the_locale(locale, expected):
    assert direction_for(locale) == expected


def test_an_explicit_direction_wins():
    assert direction_for("en", "rtl") == "rtl"


def test_resolving_sets_direction_from_the_locale():
    b = resolve(Branding(), {"locale": "ar"}, None)
    assert b.direction == "rtl"


# ── layering ──────────────────────────────────────────────────────────────────
def test_surface_beats_tenant_beats_default():
    out = resolve(Branding(name="Default"), {"name": "Tenant"}, {"name": "Surface"})
    assert out.name == "Surface"


def test_a_surface_inherits_what_it_does_not_override():
    """Restating everything per surface is what makes four UIs drift."""
    out = resolve(Branding(), {"logo_url": "https://a.co/l.png", "name": "Acme"},
                  {"primary_color": "#111"})
    assert out.logo_url == "https://a.co/l.png" and out.name == "Acme"
    assert out.primary_color == "#111"


def test_an_invalid_tenant_brand_is_refused_at_resolve_too():
    with pytest.raises(BrandingError):
        resolve(Branding(), {"primary_color": "red;}"}, None)
