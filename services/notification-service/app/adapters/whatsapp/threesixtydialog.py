"""360dialog WhatsApp Cloud API (BSP) — POST {base}/messages, a template or interactive message.

360dialog is a Meta Business Solution Provider that fronts the same Cloud API. The wire body is
Meta-shaped to the byte (messaging_product / to / type / template|interactive), so this adapter is
MetaWhatsApp with two differences that matter:

  * AUTH is the channel API key in a `D360-API-KEY` header, NOT a Bearer token. The key both
    authenticates AND selects the sending number — so, unlike Meta, the URL carries no phone_number_id
    (the endpoint is a flat `/messages`). One key == one WhatsApp channel.
  * BILLING is ours. Meta-direct sends are paid by the shop straight to Meta; a 360dialog send is
    billed to the platform account and passed through at Meta's rate. That is why `driver` is listed
    in core.provider_billing as a provider-billed driver — the cost tracker records units for it,
    and records nothing for `meta`.

The base host is config (WHATSAPP_360DIALOG_BASE_URL), never a request, so a caller cannot point a
send at a host of their choosing; the host is allow-listed at the http layer like every provider.
The success response is Meta-shaped too — messages[0].id is the wamid, the ONLY join key to the
delivery receipt — so the id is returned exactly as MetaWhatsApp returns it.
"""
from __future__ import annotations

import json as _json

from app.adapters.notify_http import ok, post_json
from app.ports.errors import ConfigError

_DEFAULT_BASE = "https://waba-v2.360dialog.io"


class ThreeSixtyDialogWhatsApp:
    driver = "threesixtydialog"

    def __init__(self, api_key: str, base_url: str = "") -> None:
        if not api_key:
            raise ConfigError(
                "WHATSAPP_360DIALOG_API_KEY is required for the threesixtydialog provider")
        self._base = (base_url or _DEFAULT_BASE).rstrip("/")
        # 360dialog authenticates with the channel key in its own header, not Authorization: Bearer.
        self._headers = {"D360-API-KEY": api_key}

    def send(self, to: str, template_name: str, language: str, variables: list[str],
             biz_opaque_callback_data: str = "") -> str:
        template: dict = {"name": template_name, "language": {"code": language or "en"}}
        if variables:
            template["components"] = [{
                "type": "body",
                "parameters": [{"type": "text", "text": str(v)} for v in variables],
            }]
        payload: dict = {"messaging_product": "whatsapp", "to": to, "type": "template",
                         "template": template}
        if biz_opaque_callback_data:
            # Echoed verbatim in the status webhook (statuses[].biz_opaque_callback_data) when the BSP
            # forwards it, so a caller can correlate a receipt against its own send id. Meta caps 512.
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

    def _post(self, payload: dict) -> str:
        # No phone_number_id in the path: the D360-API-KEY already binds the request to one channel.
        status, body = post_json(f"{self._base}/messages", payload, self._headers)
        if not ok(status):
            return ""
        # messages[0].id is the wamid — the ONLY join key to the receipt. Return it verbatim.
        try:
            return str(_json.loads(body).get("messages", [{}])[0].get("id", "") or "")
        except (ValueError, IndexError, AttributeError, KeyError):
            # Accepted but unparseable body: the send succeeded, we just have no id to match on. A
            # non-empty marker keeps it TRUTHY (a success) without pretending to be a real wamid.
            return "wamid:unparsed"
