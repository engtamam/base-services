"""Console email provider — records the send to the log. Zero infra (default)."""
from __future__ import annotations

from app.observability.logging import get_logger

_log = get_logger("app.email.console")


class ConsoleEmail:
    driver = "console"

    def send(self, to: str, subject: str, html: str, text: str, sender: str) -> bool:
        _log.info("email_sent", extra={"provider": "console", "to": to, "subject": subject})
        return True
