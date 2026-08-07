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

    def send(self, to: str, template_name: str, language: str, variables: list[str]) -> bool:
        status, _ = post_json(
            self._url,
            {"to": to, "template": template_name, "language": language, "variables": variables},
            self._headers)
        return ok(status)
