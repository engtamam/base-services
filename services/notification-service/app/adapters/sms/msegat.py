"""Msegat SMS (KSA) — POST https://www.msegat.com/gw/sendsms.php (JSON)."""
from __future__ import annotations

from app.adapters.notify_http import ok, post_json
from app.ports.errors import ConfigError

_URL = "https://www.msegat.com/gw/sendsms.php"


class MsegatSms:
    driver = "msegat"

    def __init__(self, username: str, api_key: str, sender: str) -> None:
        if not (username and api_key and sender):
            raise ConfigError("MSEGAT_USERNAME, MSEGAT_API_KEY, MSEGAT_SENDER are required")
        self._username, self._api_key, self._sender = username, api_key, sender

    def send(self, to: str, message: str) -> bool:
        payload = {
            "userName": self._username,
            "apiKey": self._api_key,
            "numbers": to,
            "userSender": self._sender,
            "msg": message,
        }
        status, _ = post_json(_URL, payload)
        return ok(status)
