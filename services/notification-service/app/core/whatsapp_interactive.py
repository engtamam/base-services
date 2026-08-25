"""Build a Meta 'interactive' message object (reply buttons or a selectable list) from typed inputs.

Interactive messages are the safe way to drive a state change over WhatsApp: the customer TAPS a
button or list row, and the reply arrives as a webhook carrying the exact `id` we set here — never a
free-text guess or an LLM's inference. So the ids ARE the contract, and this builder is where their
integrity is enforced: unique, present, within Meta's length limits.

It enforces Meta's own limits HERE, at build time, so an over-length title or a fourth button is a
clear local error rather than an opaque Cloud API 400 the caller has to reverse-engineer. Pure:
typed inputs -> dict, no I/O, no network — trivially testable and identical for every provider.

Verified against the WhatsApp Cloud API (Aug 2026), interactive messages:
  buttons — body ≤1024, footer ≤60, header ≤60, ≤3 buttons (reply.title ≤20, reply.id ≤256);
  list    — action.button ≤20, ≤10 sections (title ≤24), ≤10 rows TOTAL (title ≤24, desc ≤72, id ≤200).
"""
from __future__ import annotations

from typing import Any

_BODY_MAX, _FOOTER_MAX, _HEADER_MAX = 1024, 60, 60
_BTN_TITLE_MAX, _BTN_ID_MAX, _BTN_MAX = 20, 256, 3
_LIST_BUTTON_MAX = 20
_SECTION_TITLE_MAX, _SECTIONS_MAX = 24, 10
_ROW_TITLE_MAX, _ROW_DESC_MAX, _ROW_ID_MAX, _ROWS_MAX = 24, 72, 200, 10


class InteractiveError(ValueError):
    """An interactive spec the Cloud API would reject — caught at build time, not at send."""


def _capped(name: str, value: str, maxlen: int) -> str:
    if len(value) > maxlen:
        raise InteractiveError(f"{name} exceeds {maxlen} characters ({len(value)} given)")
    return value


def _envelope(body: str, header: str, footer: str) -> dict[str, Any]:
    if not body or not body.strip():
        raise InteractiveError("body text is required")
    block: dict[str, Any] = {"body": {"text": _capped("body", body, _BODY_MAX)}}
    if header:
        block["header"] = {"type": "text", "text": _capped("header", header, _HEADER_MAX)}
    if footer:
        block["footer"] = {"text": _capped("footer", footer, _FOOTER_MAX)}
    return block


def build_button(*, body: str, buttons: list[tuple[str, str]],
                 header: str = "", footer: str = "") -> dict[str, Any]:
    """A reply-buttons interactive object. `buttons` is [(id, title), …], at most three.

    The `id` of each button is what comes back in the customer's reply webhook — it is the anchor a
    state change keys on, so ids must be present and unique.
    """
    if not buttons:
        raise InteractiveError("a button message needs at least one button")
    if len(buttons) > _BTN_MAX:
        raise InteractiveError(f"at most {_BTN_MAX} reply buttons ({len(buttons)} given)")
    seen: set[str] = set()
    action = []
    for bid, title in buttons:
        if not bid or not title:
            raise InteractiveError("each button needs a non-empty id and title")
        if bid in seen:
            raise InteractiveError(f"duplicate button id {bid!r} — reply ids must be unique")
        seen.add(bid)
        action.append({"type": "reply", "reply": {
            "id": _capped("button id", bid, _BTN_ID_MAX),
            "title": _capped("button title", title, _BTN_TITLE_MAX)}})
    return {"type": "button", **_envelope(body, header, footer), "action": {"buttons": action}}


def build_list(*, body: str, button_text: str, sections: list[dict[str, Any]],
               header: str = "", footer: str = "") -> dict[str, Any]:
    """A selectable-list interactive object. `sections` is [{title?, rows:[{id,title,description?}]}].

    Row `id`s are the reply anchor, exactly like button ids — unique and present across the whole
    list, and Meta caps the TOTAL rows at ten however they are split into sections.
    """
    if not button_text:
        raise InteractiveError("a list message needs a button label")
    if not sections:
        raise InteractiveError("a list message needs at least one section")
    if len(sections) > _SECTIONS_MAX:
        raise InteractiveError(f"at most {_SECTIONS_MAX} sections ({len(sections)} given)")
    seen: set[str] = set()
    total = 0
    out: list[dict[str, Any]] = []
    for sec in sections:
        rows = sec.get("rows") or []
        if not rows:
            raise InteractiveError("each section needs at least one row")
        built_rows = []
        for r in rows:
            rid, rtitle = r.get("id", ""), r.get("title", "")
            if not rid or not rtitle:
                raise InteractiveError("each row needs a non-empty id and title")
            if rid in seen:
                raise InteractiveError(f"duplicate row id {rid!r} — selection ids must be unique")
            seen.add(rid)
            row: dict[str, Any] = {"id": _capped("row id", rid, _ROW_ID_MAX),
                                   "title": _capped("row title", rtitle, _ROW_TITLE_MAX)}
            if r.get("description"):
                row["description"] = _capped("row description", r["description"], _ROW_DESC_MAX)
            built_rows.append(row)
            total += 1
        section: dict[str, Any] = {"rows": built_rows}
        if sec.get("title"):
            section["title"] = _capped("section title", sec["title"], _SECTION_TITLE_MAX)
        out.append(section)
    if total > _ROWS_MAX:
        raise InteractiveError(f"at most {_ROWS_MAX} rows across all sections ({total} given)")
    return {"type": "list", **_envelope(body, header, footer),
            "action": {"button": _capped("list button", button_text, _LIST_BUTTON_MAX),
                       "sections": out}}
