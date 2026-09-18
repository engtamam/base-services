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
# A Flow message's own call-to-action button — same 20-char cap Meta applies to a reply button's
# title, because it renders the same way on the customer's screen.
_FLOW_CTA_MAX = 20
_FLOW_TOKEN_MAX, _FLOW_ID_MAX, _FLOW_SCREEN_MAX = 256, 128, 128

# Product messages draw from a connected catalog (see whatsapp_catalog): the message carries only
# IDS (catalog_id + product_retailer_id), never product data — Meta renders the image/title/price
# from the catalog at delivery time, so a price change in the catalog is reflected without resending.
# A multi-product message shows AT MOST 30 items total across its sections (Meta's hard cap).
_PRODUCT_LIST_MAX = 30


class InteractiveError(ValueError):
    """An interactive spec the Cloud API would reject — caught at build time, not at send."""


def _capped(name: str, value: str, maxlen: int) -> str:
    if len(value) > maxlen:
        raise InteractiveError(f"{name} exceeds {maxlen} characters ({len(value)} given)")
    return value


# An interactive header may be TEXT or a MEDIA object; a caller sends one or the other, never both.
# Media shape (verified against Meta's interactive-message docs, Sep 2026): {"type":"image","image":
# {"id":<media_id>}} — likewise "video"/"document". The media_id comes from a prior upload.
_HEADER_MEDIA_TYPES = ("image", "video", "document")


def _envelope(body: str, header: str, footer: str,
              header_media: dict[str, Any] | None = None) -> dict[str, Any]:
    if not body or not body.strip():
        raise InteractiveError("body text is required")
    block: dict[str, Any] = {"body": {"text": _capped("body", body, _BODY_MAX)}}
    hm = dict(header_media or {})
    if hm and header:
        raise InteractiveError("an interactive header is text OR media, not both")
    if hm:
        mtype = str(hm.get("type", "")).lower()
        if mtype not in _HEADER_MEDIA_TYPES:
            raise InteractiveError(f"a media header type must be one of {_HEADER_MEDIA_TYPES}")
        media_id = str(hm.get("media_id", "")).strip()
        if not media_id:
            raise InteractiveError("a media header needs a media_id")
        block["header"] = {"type": mtype, mtype: {"id": media_id}}
    elif header:
        block["header"] = {"type": "text", "text": _capped("header", header, _HEADER_MAX)}
    if footer:
        block["footer"] = {"text": _capped("footer", footer, _FOOTER_MAX)}
    return block


def build_button(*, body: str, buttons: list[tuple[str, str]],
                 header: str = "", footer: str = "",
                 header_media: dict[str, Any] | None = None) -> dict[str, Any]:
    """A reply-buttons interactive object. `buttons` is [(id, title), …], at most three.

    The `id` of each button is what comes back in the customer's reply webhook — it is the anchor a
    state change keys on, so ids must be present and unique.

    `header_media` optionally puts an IMAGE/VIDEO/DOCUMENT at the top instead of a text `header`
    ({"type":"image"|"video"|"document","media_id":"<id>"}) — for "photo + text + one button" outside
    a template, in the 24h window (a single photographed service; 2+ go through the carousel route).
    Text header and media header are mutually exclusive.
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
    return {"type": "button", **_envelope(body, header, footer, header_media),
            "action": {"buttons": action}}


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


def build_location_request(*, body: str) -> dict[str, Any]:
    """A location-request interactive object — body text plus a single 'Send location' button that
    opens WhatsApp's own location-sharing screen. The customer's shared pin arrives back as an
    ordinary `location` message webhook (latitude/longitude, and optionally name/address), so there
    is no reply id to anchor here the way a button or flow has: the NEXT inbound location IS the
    answer, and the caller correlates it by the conversation, not by an echoed token.

    Verified against Meta's WhatsApp Cloud API "Location request messages" (Sep 2026): interactive
    type is exactly "location_request_message", the only content is body.text (≤1024), and the action
    is the fixed {"name": "send_location"} — no header, footer, or other action fields are accepted."""
    if not body or not body.strip():
        raise InteractiveError("a location request needs body text")
    return {
        "type": "location_request_message",
        "body": {"text": _capped("body", body, _BODY_MAX)},
        "action": {"name": "send_location"},
    }


def build_flow(*, body: str, flow_token: str, flow_cta: str, screen: str = "",
               flow_id: str = "", flow_name: str = "", flow_action: str = "navigate",
               data: dict[str, Any] | None = None,
               header: str = "", footer: str = "") -> dict[str, Any]:
    """A WhatsApp Flow interactive object — a form that opens INSIDE WhatsApp and returns its answers
    in one structured reply, rather than a free-text message the customer might send about something
    else entirely (a post-visit rating's reason for using a Flow: one screen, stars AND a comment,
    delivered together and unambiguous).

    `flow_token` IS THE ANCHOR, exactly like a button's `id` — it is what the completion webhook
    echoes back, so the caller can tell which of ITS OWN pending questions this answer is for. It must
    be unique per send the same way a button id must be, and carries no meaning to Meta; only the
    caller reads it. `data` seeds the flow's first screen (e.g. a known customer name) — optional, and
    never a substitute for validating what actually comes back: a caller must not trust a value here
    as if the customer had confirmed it.

    A flow is identified by EITHER flow_id OR flow_name, never both — Meta requires exactly one, and
    flow_name is the WABA-portable identifier: a Flow cloned into each tenant's own WABA keeps its name
    but gets a new id per clone, so referencing by name lets one send definition work across every
    tenant (the same reasoning that makes templates reference a flow-button by name).

    Verified against Meta's own WhatsApp Cloud API "Sending a Flow" guide (Sep 2026): flow_message_
    version must be exactly "3"; exactly one of flow_id / flow_name identifies the flow; flow_cta is
    required; flow_action_payload.data is optional but MUST be non-empty if present — hence omitted
    here when empty rather than sent as {}.

    `flow_action` is "navigate" (default, STATIC flow) or "data_exchange" (DYNAMIC flow driven by the
    encrypted endpoint — the booking calendar is one). For navigate, the first `screen` is REQUIRED
    (the client renders it directly). For data_exchange, opening the flow triggers an INIT call to the
    endpoint which RETURNS the first screen, so `screen` is OPTIONAL — omitted here when empty, and the
    whole flow_action_payload is omitted when neither a screen nor seed data is supplied (Meta rejects
    an empty payload)."""
    if not flow_token:
        raise InteractiveError("a flow message needs a flow_token — it is the reply's only anchor")
    if bool(flow_id) == bool(flow_name):
        raise InteractiveError("a flow message needs exactly one of flow_id or flow_name")
    if not flow_cta:
        raise InteractiveError("a flow message needs a call-to-action label")
    if flow_action not in ("navigate", "data_exchange"):
        raise InteractiveError("flow_action must be 'navigate' or 'data_exchange'")
    if flow_action == "navigate" and not screen:
        raise InteractiveError("a navigate flow needs the first screen's name")
    # flow_action_payload.data is OPTIONAL but "must be NON-EMPTY if provided" (Meta spec), so it is
    # OMITTED when empty. For data_exchange the endpoint's INIT supplies the first screen, so screen
    # may be absent; the payload is then omitted entirely rather than sent as an empty {}.
    payload: dict[str, Any] = {}
    if screen:
        payload["screen"] = _capped("screen", screen, _FLOW_SCREEN_MAX)
    if data:
        payload["data"] = dict(data)
    parameters: dict[str, Any] = {
        "flow_message_version": "3",
        "flow_token": _capped("flow_token", flow_token, _FLOW_TOKEN_MAX),
        "flow_cta": _capped("flow_cta", flow_cta, _FLOW_CTA_MAX),
        "flow_action": flow_action,
    }
    if payload:
        parameters["flow_action_payload"] = payload
    # Exactly one identifier (enforced above). flow_name is the clone-portable one; flow_id the direct.
    if flow_id:
        parameters["flow_id"] = _capped("flow_id", flow_id, _FLOW_ID_MAX)
    else:
        parameters["flow_name"] = _capped("flow_name", flow_name, _FLOW_ID_MAX)
    return {
        "type": "flow", **_envelope(body, header, footer),
        "action": {"name": "flow", "parameters": parameters},
    }


def build_product(*, catalog_id: str, product_retailer_id: str,
                  body: str = "", footer: str = "") -> dict[str, Any]:
    """A Single Product Message — one catalog item rendered with its image, title and price, plus an
    'Add to cart' affordance the customer uses to build an order (which returns as an inbound `order`
    webhook, captured by the durable inbound feed).

    The message carries only IDS: `catalog_id` (the catalog connected to the sending number) and
    `product_retailer_id` (the item's SKU/retailer id in that catalog, i.e. the `id` we set in
    whatsapp_catalog.sync_products). Meta renders the product from the catalog at delivery, so this
    never carries a price we could get wrong.

    Verified against Meta's WhatsApp Cloud API "Single Product Messages" (Sep 2026): interactive
    `type` is exactly "product"; `action` carries catalog_id + product_retailer_id; body and footer
    are OPTIONAL and a single-product message takes NO header (unlike product_list). Valid only inside
    the 24h customer-service window or as a template — the same window rule as every interactive send.
    """
    if not catalog_id or not catalog_id.strip():
        raise InteractiveError("a product message needs a catalog_id")
    if not product_retailer_id or not product_retailer_id.strip():
        raise InteractiveError("a product message needs a product_retailer_id")
    block: dict[str, Any] = {
        "type": "product",
        "action": {"catalog_id": catalog_id, "product_retailer_id": product_retailer_id},
    }
    # body/footer are optional here; include only when supplied (an empty body{} is not accepted).
    if body and body.strip():
        block["body"] = {"text": _capped("body", body, _BODY_MAX)}
    if footer:
        block["footer"] = {"text": _capped("footer", footer, _FOOTER_MAX)}
    return block


def build_product_list(*, catalog_id: str, sections: list[dict[str, Any]],
                       header: str, body: str, footer: str = "") -> dict[str, Any]:
    """A Multi-Product Message — up to 30 catalog items grouped into sections, from which the customer
    picks quantities and sends back ONE inbound `order` webhook.

    `sections` is [{"title": str, "product_ids": [retailer_id, …]}, …]. Each product_id is a
    `product_retailer_id` in the connected catalog. As with the single-product message, only IDS are
    sent — Meta renders each item from the catalog.

    Verified against Meta's WhatsApp Cloud API "Multi-Product Messages" (Sep 2026): interactive `type`
    is exactly "product_list"; a text `header` is REQUIRED (it is the only header type accepted here)
    and so is `body`; `action` carries catalog_id + sections[], each section {title, product_items:
    [{product_retailer_id}]}; the message shows AT MOST 30 products TOTAL across all sections. A section
    title is required when there is more than one section, so the customer can tell the groups apart.
    """
    if not catalog_id or not catalog_id.strip():
        raise InteractiveError("a product-list message needs a catalog_id")
    if not header or not header.strip():
        raise InteractiveError("a product-list message requires a text header")
    if not body or not body.strip():
        raise InteractiveError("a product-list message requires body text")
    if not sections:
        raise InteractiveError("a product-list message needs at least one section")
    multi_section = len(sections) > 1
    total = 0
    out: list[dict[str, Any]] = []
    for sec in sections:
        product_ids = sec.get("product_ids") or []
        if not product_ids:
            raise InteractiveError("each section needs at least one product_id")
        title = sec.get("title", "")
        if multi_section and not title:
            raise InteractiveError("each section needs a title when there is more than one section")
        items = []
        for pid in product_ids:
            if not pid or not str(pid).strip():
                raise InteractiveError("each product_id must be non-empty")
            items.append({"product_retailer_id": str(pid)})
            total += 1
        section: dict[str, Any] = {"product_items": items}
        if title:
            section["title"] = _capped("section title", title, _SECTION_TITLE_MAX)
        out.append(section)
    if total > _PRODUCT_LIST_MAX:
        raise InteractiveError(
            f"a product-list message shows at most {_PRODUCT_LIST_MAX} products ({total} given)")
    block: dict[str, Any] = {
        "type": "product_list",
        "header": {"type": "text", "text": _capped("header", header, _HEADER_MAX)},
        "body": {"text": _capped("body", body, _BODY_MAX)},
        "action": {"catalog_id": catalog_id, "sections": out},
    }
    if footer:
        block["footer"] = {"text": _capped("footer", footer, _FOOTER_MAX)}
    return block


def parse_order(message: dict[str, Any]) -> dict[str, Any] | None:
    """Normalise an inbound `order` message (the cart a customer sends back) into a stable shape the
    order consumer can use directly, or None if this is not an order.

    Meta's inbound order shape (verified Sep 2026): a message with `type:"order"` and
    `order: {catalog_id, text?, product_items: [{product_retailer_id, quantity, item_price, currency}]}`.
    `item_price` is a MAJOR-unit number (e.g. 12.50) and `quantity` may arrive as a string — both are
    footguns every consumer would otherwise re-handle, so they are normalised ONCE here: quantity to
    int, and a `line_total` computed as price × quantity. Raw values are preserved alongside so nothing
    is lost. Pure and I/O-free — the durable feed keeps the original message; this is a reader helper.
    """
    if not isinstance(message, dict) or message.get("type") != "order":
        return None
    order = message.get("order") or {}
    items: list[dict[str, Any]] = []
    for it in order.get("product_items", []) or []:
        try:
            qty = int(it.get("quantity", 0) or 0)
        except (TypeError, ValueError):
            qty = 0
        try:
            price = float(it.get("item_price", 0) or 0)
        except (TypeError, ValueError):
            price = 0.0
        items.append({
            "product_retailer_id": str(it.get("product_retailer_id", "")),
            "quantity": qty,
            "item_price": price,
            "currency": str(it.get("currency", "")),
            "line_total": round(price * qty, 4),
        })
    return {
        "catalog_id": str(order.get("catalog_id", "")),
        "text": str(order.get("text", "") or ""),
        "from": str(message.get("from", "")),
        "wamid": str(message.get("id", "")),
        "items": items,
        "order_total": round(sum(i["line_total"] for i in items), 4),
    }
