"""Branding — the widget's look, set by an operator rather than compiled in.

An adopter runs several UIs and wants the assistant to look like their product in each. That has to
be configuration, because the alternative is a rebuild per customer, which is how a plug-and-play
component stops being plug-and-play.

EVERY VALUE IS VALIDATED, AND THAT IS A SECURITY CONTROL RATHER THAN TIDINESS. Branding is the
perfect XSS carrier: it is attacker-adjacent (an admin panel field), it is stored, it is served to
every user of that tenant, and nobody thinks of it as untrusted input because "it is just our
colours". A `logo_url` of `javascript:…`, or an accent colour of `red;} body{display:none` injected
into a stylesheet, is a defect in every adopter's application at once. So:

  * colours must match a strict hex pattern — not "look like" one
  * URLs must be http(s) and are refused otherwise, which rules out `javascript:` and `data:`
  * text is length-capped and stripped of angle brackets, because a greeting is prose and prose
    never needs markup

The widget also renders these into a shadow DOM, so a value that somehow got through cannot reach
the host page's styles. Two layers, because the outer one is operator-supplied and operators paste.

RESOLUTION IS SPECIFIC-BEATS-GENERAL: a per-surface brand overrides a per-tenant one, which
overrides the deployment default. An adopter therefore configures once and refines only where a
surface genuinely differs — an admin console and a customer app should not have to be described
separately just to share a logo.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

# #rgb, #rrggbb, #rrggbbaa — anchored, so "red;}body{…" cannot pass as a colour.
_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_MAX_TEXT = 200
_MAX_URL = 2048

# Where the launcher sits. A closed set: an arbitrary string here would end up in a style rule.
POSITIONS = ("bottom-right", "bottom-left", "top-right", "top-left")
DIRECTIONS = ("ltr", "rtl")


class BrandingError(ValueError):
    """A brand value that cannot be rendered safely. Raised when it is SAVED, so the operator who
    typed it learns immediately, rather than every user of that tenant discovering it later."""


def _colour(value: str, field_name: str) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    if not _HEX.match(v):
        raise BrandingError(
            f"{field_name}: {value!r} is not a hex colour. Anything else would be interpolated "
            f"into a stylesheet, which is how a colour field becomes a script injection.")
    return v


def _url(value: str, field_name: str) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    if len(v) > _MAX_URL:
        raise BrandingError(f"{field_name}: URL longer than {_MAX_URL} characters")
    low = v.lower()
    if not (low.startswith("https://") or low.startswith("http://")):
        # Refusing anything else is what rules out javascript: and data: without having to
        # enumerate every scheme somebody might try.
        raise BrandingError(
            f"{field_name}: must start with http:// or https:// — {value[:40]!r} does not, and a "
            f"scheme we did not choose is how a logo field becomes script execution.")
    return v


def _text(value: str, field_name: str) -> str:
    v = (value or "").strip()
    if len(v) > _MAX_TEXT:
        raise BrandingError(f"{field_name}: longer than {_MAX_TEXT} characters")
    if "<" in v or ">" in v:
        # A greeting is prose. Prose never needs markup, so refusing is cheaper and safer than
        # escaping — and refusing tells the operator, where escaping silently mangles their text.
        raise BrandingError(f"{field_name}: must not contain '<' or '>' — it is displayed as text")
    return v


@dataclass(frozen=True)
class Branding:
    name: str = "Assistant"
    greeting: str = ""
    primary_color: str = ""
    surface_color: str = ""
    text_color: str = ""
    logo_url: str = ""
    position: str = "bottom-right"
    direction: str = ""              # "" = derive from the locale
    locale: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "greeting": self.greeting,
                "primary_color": self.primary_color, "surface_color": self.surface_color,
                "text_color": self.text_color, "logo_url": self.logo_url,
                "position": self.position, "direction": self.direction, "locale": self.locale}


# Languages written right to left. Direction is a LAYOUT fact, not a translation: an Arabic panel
# whose launcher, close button and message alignment stay on the left reads as broken software,
# however good the translation is.
_RTL_LANGS = frozenset({"ar", "he", "fa", "ur", "ps", "sd", "yi"})


def direction_for(locale: str, explicit: str = "") -> str:
    """RTL or LTR. An explicit setting wins; otherwise the locale decides."""
    if explicit in DIRECTIONS:
        return explicit
    base = (locale or "").strip().lower().split("-", 1)[0]
    return "rtl" if base in _RTL_LANGS else "ltr"


def parse(raw: dict, *, base: Branding | None = None) -> Branding:
    """Validate operator input into a Branding, refusing anything unrenderable.

    Unknown keys are IGNORED rather than rejected, so an adopter sending a richer object from a
    newer widget does not break on an older service — but nothing unknown is ever stored or served.
    """
    if not isinstance(raw, dict):
        raise BrandingError("branding must be an object")
    b = base or Branding()
    position = (raw.get("position") or b.position or "bottom-right").strip()
    if position not in POSITIONS:
        raise BrandingError(f"position: must be one of {', '.join(POSITIONS)}")
    direction = (raw.get("direction") or b.direction or "").strip().lower()
    if direction and direction not in DIRECTIONS:
        raise BrandingError(f"direction: must be one of {', '.join(DIRECTIONS)}")
    return Branding(
        name=_text(raw.get("name", b.name), "name") or "Assistant",
        greeting=_text(raw.get("greeting", b.greeting), "greeting"),
        primary_color=_colour(raw.get("primary_color", b.primary_color), "primary_color"),
        surface_color=_colour(raw.get("surface_color", b.surface_color), "surface_color"),
        text_color=_colour(raw.get("text_color", b.text_color), "text_color"),
        logo_url=_url(raw.get("logo_url", b.logo_url), "logo_url"),
        position=position,
        direction=direction,
        locale=_text(raw.get("locale", b.locale), "locale"),
    )


def resolve(default: Branding, per_tenant: dict | None, per_surface: dict | None,
            *, locale: str = "") -> Branding:
    """Specific beats general: surface over tenant over deployment default.

    Layered rather than replaced, so a surface that only wants a different accent colour inherits
    the logo and the name instead of restating them — restating is what makes four UIs drift.
    """
    out = default
    if per_tenant:
        out = parse(per_tenant, base=out)
    if per_surface:
        out = parse(per_surface, base=out)
    if locale and not out.locale:
        out = replace(out, locale=locale)
    return replace(out, direction=direction_for(out.locale or locale, out.direction))
