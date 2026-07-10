"""Generic webhook SMS — POST {to, message} JSON to any gateway you run/proxy.

Escape hatch so ANY provider (OurSMS, 4jawaly, Yamamah, a relay, ...) can be wired
by config without shipping a guessed endpoint. URL host must be allow-listed.
"""
from __future__ import annotations

from app.adapters.notify_http import bearer, ok, post_json
from app.ports.errors import ConfigError


class WebhookSms:
    driver = "webhook"

    def __init__(self, url: str, token: str = "") -> None:
        if not url:
            raise ConfigError("SMS_WEBHOOK_URL is required for the webhook provider")
        self._url = url
        self._headers = bearer(token) if token else {}

    def send(self, to: str, message: str) -> bool:
        status, _ = post_json(self._url, {"to": to, "message": message}, self._headers)
        return ok(status)
