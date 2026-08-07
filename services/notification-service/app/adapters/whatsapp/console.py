"""Console WhatsApp provider — records the send to the log. Zero infra (default)."""
from __future__ import annotations

from app.observability.logging import get_logger

_log = get_logger("app.whatsapp.console")


class ConsoleWhatsApp:
    driver = "console"

    def send(self, to: str, template_name: str, language: str, variables: list[str]) -> bool:
        _log.info("whatsapp_sent", extra={"provider": "console", "to": to,
                                          "template": template_name, "language": language})
        return True
