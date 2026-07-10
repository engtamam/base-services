"""SendGrid email — POST https://api.sendgrid.com/v3/mail/send (Bearer; 202)."""
from __future__ import annotations

from app.adapters.notify_http import bearer, ok, post_json
from app.ports.errors import ConfigError

_URL = "https://api.sendgrid.com/v3/mail/send"


class SendgridEmail:
    driver = "sendgrid"

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ConfigError("SENDGRID_API_KEY is required for the sendgrid provider")
        self._key = api_key

    def send(self, to: str, subject: str, html: str, text: str, sender: str) -> bool:
        content = []
        if text:
            content.append({"type": "text/plain", "value": text})
        if html:
            content.append({"type": "text/html", "value": html})
        if not content:
            content.append({"type": "text/plain", "value": ""})
        payload = {
            "personalizations": [{"to": [{"email": to}]}],
            "from": {"email": sender},
            "subject": subject,
            "content": content,
        }
        status, _ = post_json(_URL, payload, bearer(self._key))
        return ok(status)  # 202 Accepted
