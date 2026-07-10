"""Postmark email — POST https://api.postmarkapp.com/email (X-Postmark-Server-Token)."""
from __future__ import annotations

from app.adapters.notify_http import ok, post_json
from app.ports.errors import ConfigError

_URL = "https://api.postmarkapp.com/email"


class PostmarkEmail:
    driver = "postmark"

    def __init__(self, token: str, stream: str) -> None:
        if not token:
            raise ConfigError("POSTMARK_TOKEN is required for the postmark provider")
        self._headers = {"X-Postmark-Server-Token": token, "Accept": "application/json"}
        self._stream = stream or "outbound"

    def send(self, to: str, subject: str, html: str, text: str, sender: str) -> bool:
        payload = {"From": sender, "To": to, "Subject": subject, "MessageStream": self._stream}
        if text:
            payload["TextBody"] = text
        if html:
            payload["HtmlBody"] = html
        status, _ = post_json(_URL, payload, self._headers)
        return ok(status)
