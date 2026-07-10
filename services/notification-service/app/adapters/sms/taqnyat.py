"""Taqnyat SMS (KSA) — POST https://api.taqnyat.sa/v1/messages (Bearer, JSON)."""
from __future__ import annotations

from app.adapters.notify_http import bearer, ok, post_json
from app.ports.errors import ConfigError

_URL = "https://api.taqnyat.sa/v1/messages"


class TaqnyatSms:
    driver = "taqnyat"

    def __init__(self, token: str, sender: str) -> None:
        if not (token and sender):
            raise ConfigError("TAQNYAT_TOKEN and TAQNYAT_SENDER are required")
        self._headers = bearer(token)
        self._sender = sender

    def send(self, to: str, message: str) -> bool:
        payload = {"recipients": [to], "body": message, "sender": self._sender}
        status, _ = post_json(_URL, payload, self._headers)
        return ok(status)  # 201 Created
