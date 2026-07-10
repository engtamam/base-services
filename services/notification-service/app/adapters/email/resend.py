"""Resend email — POST https://api.resend.com/emails (Bearer; User-Agent required)."""
from __future__ import annotations

from app.adapters.notify_http import bearer, ok, post_json
from app.ports.errors import ConfigError

_URL = "https://api.resend.com/emails"


class ResendEmail:
    driver = "resend"

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ConfigError("RESEND_API_KEY is required for the resend provider")
        self._key = api_key

    def send(self, to: str, subject: str, html: str, text: str, sender: str) -> bool:
        payload = {"from": sender, "to": [to], "subject": subject}
        if html:
            payload["html"] = html
        if text:
            payload["text"] = text
        status, _ = post_json(_URL, payload, bearer(self._key))
        return ok(status)
