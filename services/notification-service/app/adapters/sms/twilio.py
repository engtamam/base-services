"""Twilio SMS (global) — POST .../Accounts/{SID}/Messages.json (Basic auth, form)."""
from __future__ import annotations

from app.adapters.notify_http import basic, ok, post_form
from app.ports.errors import ConfigError


class TwilioSms:
    driver = "twilio"

    def __init__(self, account_sid: str, auth_token: str, sender: str) -> None:
        if not (account_sid and auth_token and sender):
            raise ConfigError("TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM are required")
        self._url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
        self._auth = basic(account_sid, auth_token)
        self._from = sender

    def send(self, to: str, message: str) -> bool:
        status, _ = post_form(self._url, {"To": to, "From": self._from, "Body": message}, self._auth)
        return ok(status)
