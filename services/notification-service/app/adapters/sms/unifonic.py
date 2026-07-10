"""Unifonic SMS (KSA) — POST https://el.cloud.unifonic.com/rest/SMS/messages (form)."""
from __future__ import annotations

from app.adapters.notify_http import ok, post_form
from app.ports.errors import ConfigError

_URL = "https://el.cloud.unifonic.com/rest/SMS/messages"


class UnifonicSms:
    driver = "unifonic"

    def __init__(self, app_sid: str, sender: str) -> None:
        if not app_sid:
            raise ConfigError("UNIFONIC_APP_SID is required for the unifonic provider")
        self._app_sid, self._sender = app_sid, sender

    def send(self, to: str, message: str) -> bool:
        fields = {"AppSid": self._app_sid, "Recipient": to, "Body": message}
        if self._sender:
            fields["SenderID"] = self._sender
        status, _ = post_form(_URL, fields)
        return ok(status)
