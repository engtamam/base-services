"""Generic webhook WhatsApp — POST {to, template, language, variables} to any gateway you run/proxy.

The escape hatch (like the SMS webhook): a QR-linked personal gateway, an on-prem relay, or any
provider we do not ship a driver for can be wired by CONFIG alone. URL host must be allow-listed.
"""
from __future__ import annotations

from app.adapters.notify_http import bearer, ok, post_json
from app.ports.errors import ConfigError


class WebhookWhatsApp:
    driver = "webhook"

    def __init__(self, url: str, token: str = "") -> None:
        if not url:
            raise ConfigError("WHATSAPP_WEBHOOK_URL is required for the webhook provider")
        self._url = url
        self._headers = bearer(token) if token else {}

    def send(self, to: str, template_name: str, language: str, variables: list[str],
             biz_opaque_callback_data: str = "") -> str:
        status, _ = post_json(
            self._url,
            {"to": to, "template": template_name, "language": language, "variables": variables},
            self._headers)
        # A relay gateway is not Cloud API and returns no wamid, so there is no receipt to match. A
        # non-empty marker keeps success TRUTHY without inventing a wamid that can never resolve.
        return "webhook:sent" if ok(status) else ""

    def send_interactive(self, to: str, interactive: dict,
                         biz_opaque_callback_data: str = "") -> str:
        # Forwarded as-is to the relay: {to, interactive} mirrors Cloud API's own field so a gateway
        # proxying to Meta can pass it straight through. No wamid comes back from a relay.
        status, _ = post_json(self._url, {"to": to, "interactive": interactive}, self._headers)
        return "webhook:sent" if ok(status) else ""

    def send_text(self, to: str, body: str,
                  biz_opaque_callback_data: str = "") -> str:
        # Forwarded as-is to the relay: {to, text:{body}} mirrors Cloud API's own field so a gateway
        # proxying to Meta can pass it straight through. No wamid comes back from a relay.
        status, _ = post_json(self._url, {"to": to, "text": {"body": body}}, self._headers)
        return "webhook:sent" if ok(status) else ""
