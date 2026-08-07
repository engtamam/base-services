"""Meta WhatsApp Cloud API — POST {graph}/{phone_number_id}/messages, a template message.

Business-initiated WhatsApp requires an APPROVED template referenced by name, with its variables as
ordered body parameters. The access token and phone-number id are resolved per tenant like every
other provider credential; the graph base is config (never a request), so a caller cannot point a
send at a host of their choosing. Host is allow-listed at the http layer.
"""
from __future__ import annotations

from app.adapters.notify_http import bearer, ok, post_json
from app.ports.errors import ConfigError

_DEFAULT_BASE = "https://graph.facebook.com/v19.0"


class MetaWhatsApp:
    driver = "meta"

    def __init__(self, access_token: str, phone_number_id: str, base_url: str = "") -> None:
        if not access_token or not phone_number_id:
            raise ConfigError(
                "WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID are required for the meta provider")
        self._base = (base_url or _DEFAULT_BASE).rstrip("/")
        self._phone_id = phone_number_id
        self._headers = bearer(access_token)

    def send(self, to: str, template_name: str, language: str, variables: list[str]) -> bool:
        template: dict = {"name": template_name, "language": {"code": language or "en"}}
        if variables:
            template["components"] = [{
                "type": "body",
                "parameters": [{"type": "text", "text": str(v)} for v in variables],
            }]
        status, _ = post_json(
            f"{self._base}/{self._phone_id}/messages",
            {"messaging_product": "whatsapp", "to": to, "type": "template", "template": template},
            self._headers)
        return ok(status)
