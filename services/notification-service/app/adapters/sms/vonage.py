"""Vonage/Nexmo SMS (global) — POST https://rest.nexmo.com/sms/json (form)."""
from __future__ import annotations

from app.adapters.notify_http import ok, post_form
from app.ports.errors import ConfigError

_URL = "https://rest.nexmo.com/sms/json"


class VonageSms:
    driver = "vonage"

    def __init__(self, api_key: str, api_secret: str, sender: str) -> None:
        if not (api_key and api_secret and sender):
            raise ConfigError("VONAGE_API_KEY, VONAGE_API_SECRET, VONAGE_FROM are required")
        self._key, self._secret, self._from = api_key, api_secret, sender

    def send(self, to: str, message: str) -> bool:
        status, _ = post_form(_URL, {
            "api_key": self._key, "api_secret": self._secret,
            "to": to, "from": self._from, "text": message,
        })
        return ok(status)
