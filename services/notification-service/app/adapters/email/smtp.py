"""SMTP email provider — stdlib smtplib (works with any SMTP: Gmail/O365/SES/...)."""
from __future__ import annotations

import smtplib
from email.message import EmailMessage

from app.ports.errors import ConfigError


class SmtpEmail:
    driver = "smtp"

    def __init__(self, host: str, port: int, user: str, password: str,
                 starttls: bool, timeout: int = 15) -> None:
        if not host:
            raise ConfigError("SMTP_HOST is required for the smtp email provider")
        self._host, self._port = host, port
        self._user, self._password, self._starttls = user, password, starttls
        self._timeout = timeout

    def send(self, to: str, subject: str, html: str, text: str, sender: str) -> bool:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = sender, to, subject
        msg.set_content(text or "")
        if html:
            msg.add_alternative(html, subtype="html")
        try:
            with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as s:
                if self._starttls:
                    s.starttls()
                if self._user:
                    s.login(self._user, self._password)
                s.send_message(msg)
            return True
        except Exception:
            return False
