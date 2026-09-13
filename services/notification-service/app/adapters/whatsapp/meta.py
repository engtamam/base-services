"""Meta WhatsApp Cloud API — POST {graph}/{phone_number_id}/messages, a template message.

Business-initiated WhatsApp requires an APPROVED template referenced by name, with its variables as
ordered body parameters. The access token and phone-number id are resolved per tenant like every
other provider credential; the graph base is config (never a request), so a caller cannot point a
send at a host of their choosing. Host is allow-listed at the http layer.
"""
from __future__ import annotations

import json as _json

from app.adapters.notify_http import bearer, ok, post_json
from app.ports.errors import ConfigError

_DEFAULT_BASE = "https://graph.facebook.com/v26.0"


class MetaWhatsApp:
    driver = "meta"

    def __init__(self, access_token: str, phone_number_id: str, base_url: str = "") -> None:
        if not access_token or not phone_number_id:
            raise ConfigError(
                "WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID are required for the meta provider")
        self._base = (base_url or _DEFAULT_BASE).rstrip("/")
        self._phone_id = phone_number_id
        self._headers = bearer(access_token)

    def send(self, to: str, template_name: str, language: str, variables: list[str],
             biz_opaque_callback_data: str = "", flow_token: str = "",
             flow_action_data: dict | None = None, header_media_id: str = "") -> str:
        template: dict = {"name": template_name, "language": {"code": language or "en"}}
        components: list[dict] = []
        if header_media_id:
            # An image header on an ordinary (single-message) template — the same shape the carousel
            # path builds per card (core/whatsapp_carousel), here for a text+image+button campaign
            # message that needs no carousel. Header goes first, matching Meta's component order.
            # Declared only when supplied, exactly like flow_token below.
            components.append({"type": "header",
                               "parameters": [{"type": "image", "image": {"id": header_media_id}}]})
        if variables:
            components.append({
                "type": "body",
                "parameters": [{"type": "text", "text": str(v)} for v in variables],
            })
        if flow_token:
            # A Flow button that is a component OF THE APPROVED TEMPLATE (business-initiated, no 24h
            # window needed) — distinct from the interactive-message Flow (build_flow), which needs the
            # window. The button + its flow are declared and APPROVED at template creation; here we only
            # supply the per-send action. Verified against Meta's "Sending a Flow" guide (Sep 2026):
            # type "button", sub_type "flow", index "0" (single button), one parameter of type "action"
            # whose `action` carries flow_token and flow_action_data. flow_action_data seeds the first
            # screen and MUST be non-empty if present — so it is OMITTED when empty, never sent as {}.
            action: dict = {"flow_token": flow_token}
            if flow_action_data:
                action["flow_action_data"] = dict(flow_action_data)
            components.append({"type": "button", "sub_type": "flow", "index": "0",
                               "parameters": [{"type": "action", "action": action}]})
        if components:
            template["components"] = components
        payload: dict = {"messaging_product": "whatsapp", "to": to, "type": "template",
                         "template": template}
        if biz_opaque_callback_data:
            # Echoed verbatim in the status webhook (statuses[].biz_opaque_callback_data), so a caller
            # can correlate a receipt against its own send id with no join. Meta caps it at 512 chars.
            payload["biz_opaque_callback_data"] = biz_opaque_callback_data[:512]
        return self._post(payload)

    def send_template_components(self, to: str, template_name: str, language: str,
                                 components: list[dict], biz_opaque_callback_data: str = "") -> str:
        """Send an approved template with a PRE-BUILT components list — the rich-template path (a
        carousel's per-card component tree, built + validated upstream in core/whatsapp_carousel). The
        components are Meta's own shape and posted verbatim; same envelope + wamid handling as send()."""
        template: dict = {"name": template_name, "language": {"code": language or "en"}}
        if components:
            template["components"] = components
        payload: dict = {"messaging_product": "whatsapp", "to": to, "type": "template",
                         "template": template}
        if biz_opaque_callback_data:
            payload["biz_opaque_callback_data"] = biz_opaque_callback_data[:512]
        return self._post(payload)

    def send_interactive(self, to: str, interactive: dict,
                         biz_opaque_callback_data: str = "") -> str:
        """An interactive (button/list) message — no template, sent inside the 24h window. The
        `interactive` object is built and limit-checked upstream (core/whatsapp_interactive); here it
        is simply wrapped in the message envelope and posted through the same endpoint as a template."""
        payload: dict = {"messaging_product": "whatsapp", "to": to, "type": "interactive",
                         "interactive": interactive}
        if biz_opaque_callback_data:
            payload["biz_opaque_callback_data"] = biz_opaque_callback_data[:512]
        return self._post(payload)

    def send_text(self, to: str, body: str, biz_opaque_callback_data: str = "") -> str:
        """A plain free-text reply — no template, valid ONLY inside the 24h customer-service window
        (a real user must have messaged us first; outside it, Meta rejects a non-template send). Same
        envelope and response handling as send()/send_interactive(), just a text body."""
        payload: dict = {"messaging_product": "whatsapp", "to": to, "type": "text",
                         "text": {"body": body}}
        if biz_opaque_callback_data:
            payload["biz_opaque_callback_data"] = biz_opaque_callback_data[:512]
        return self._post(payload)

    def _post(self, payload: dict) -> str:
        status, body = post_json(f"{self._base}/{self._phone_id}/messages", payload, self._headers)
        if not ok(status):
            return ""
        # The wamid is the ONLY join key to the receipt. Discarding it (as this once did) is exactly
        # why every receipt was unattributable. messages[0].id on every accepted send.
        try:
            return str(_json.loads(body).get("messages", [{}])[0].get("id", "") or "")
        except (ValueError, IndexError, AttributeError, KeyError):
            # Accepted but unparseable body: the send succeeded, we just have no id to match on. A
            # non-empty marker keeps it TRUTHY (a success) without pretending to be a real wamid.
            return "wamid:unparsed"
