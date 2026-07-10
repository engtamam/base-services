"""Mailgun email — POST {base}/v3/{domain}/messages (HTTP Basic 'api:KEY', form)."""
from __future__ import annotations

from app.adapters.notify_http import basic, ok, post_form
from app.ports.errors import ConfigError


class MailgunEmail:
    driver = "mailgun"

    def __init__(self, api_key: str, domain: str, base: str) -> None:
        if not (api_key and domain):
            raise ConfigError("MAILGUN_API_KEY and MAILGUN_DOMAIN are required")
        self._url = f"{base.rstrip('/')}/v3/{domain}/messages"
        self._auth = basic("api", api_key)

    def send(self, to: str, subject: str, html: str, text: str, sender: str) -> bool:
        fields = {"from": sender, "to": to, "subject": subject, "text": text or ""}
        if html:
            fields["html"] = html
        status, _ = post_form(self._url, fields, self._auth)
        return ok(status)
