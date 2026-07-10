"""Mobily.ws SMS (KSA) — POST https://api.mobily.ws/api/msgSend.php (form).

applicationType=68 and lang=3 (UTF-8) are fixed per Mobily's spec. A success
response body is "1"; other values are documented error codes.
"""
from __future__ import annotations

from app.adapters.notify_http import ok, post_form
from app.ports.errors import ConfigError

_URL = "https://api.mobily.ws/api/msgSend.php"


class MobilySms:
    driver = "mobily"

    def __init__(self, mobile: str, password: str, sender: str) -> None:
        if not (mobile and password and sender):
            raise ConfigError("MOBILY_MOBILE, MOBILY_PASSWORD, MOBILY_SENDER are required")
        self._mobile, self._password, self._sender = mobile, password, sender

    def send(self, to: str, message: str) -> bool:
        status, body = post_form(_URL, {
            "mobile": self._mobile, "password": self._password,
            "numbers": to, "sender": self._sender, "msg": message,
            "applicationType": "68", "lang": "3",
        })
        # Mobily returns "1" in the body on success (HTTP 200 always).
        return ok(status) and body.strip() == "1"
