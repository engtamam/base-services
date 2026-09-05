"""Build the SEND-time component tree for an approved carousel template.

A carousel template is approved once with N cards of a fixed shape (core/whatsapp_templates validates
that at registration). SENDING it supplies each card's runtime values: the header media, the body
variable values, and the button parameters. This is a pure typed-input → Meta-dict builder, no I/O,
so it is trivially testable and identical for every provider.

Verified against Meta's carousel-template send guide (Sep 2026): a `carousel` component whose `cards`
each carry an integer `card_index` and a `components` list — a `header` parameter (image/video with a
media `id`), an optional `body` with text parameters, and `button` parameters keyed by a STRING
`index` and `sub_type` (quick_reply carries a `payload`, url carries a `text`).
"""
from __future__ import annotations

from typing import Any

_MEDIA_TYPES = ("image", "video")
_BUTTON_SUB_TYPES = ("quick_reply", "url")


class CarouselSendError(ValueError):
    """A carousel send spec the Cloud API would reject — caught at build time, not at send."""


def _card_component(card: dict[str, Any], i: int) -> dict[str, Any]:
    media_id = str(card.get("media_id") or "")
    if not media_id:
        raise CarouselSendError(f"card {i}: a media_id is required (upload the media, reference its id)")
    media_type = str(card.get("media_type") or "image").lower()
    if media_type not in _MEDIA_TYPES:
        raise CarouselSendError(f"card {i}: media_type must be one of {_MEDIA_TYPES}")
    comps: list[dict[str, Any]] = [{
        "type": "header",
        "parameters": [{"type": media_type, media_type: {"id": media_id}}],
    }]
    body_params = [str(p) for p in (card.get("body_params") or [])]
    if body_params:
        comps.append({"type": "body",
                      "parameters": [{"type": "text", "text": p} for p in body_params]})
    for idx, b in enumerate(card.get("buttons") or []):
        sub = str(b.get("sub_type") or "").lower()
        if sub not in _BUTTON_SUB_TYPES:
            raise CarouselSendError(f"card {i} button {idx}: sub_type must be one of {_BUTTON_SUB_TYPES}")
        if sub == "quick_reply":
            params = [{"type": "payload", "payload": str(b.get("payload", ""))}]
        else:  # url — the send supplies the dynamic URL suffix as a text parameter
            params = [{"type": "text", "text": str(b.get("text", ""))}]
        comps.append({"type": "button", "sub_type": sub, "index": str(idx), "parameters": params})
    return {"card_index": i, "components": comps}


def build_carousel_send(cards: list[dict[str, Any]]) -> dict[str, Any]:
    """The `carousel` send component from per-card runtime values. Raises CarouselSendError on a bad
    spec. `cards` order defines card_index (0-based), matching the approved template's card order."""
    if not cards:
        raise CarouselSendError("a carousel send needs at least one card")
    return {"type": "carousel", "cards": [_card_component(c, i) for i, c in enumerate(cards)]}
