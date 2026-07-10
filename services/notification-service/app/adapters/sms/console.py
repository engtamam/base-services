"""Console SMS provider — records the send to the log. Zero infra (default)."""
from __future__ import annotations

from app.observability.logging import get_logger

_log = get_logger("app.sms.console")


class ConsoleSms:
    driver = "console"

    def send(self, to: str, message: str) -> bool:
        _log.info("sms_sent", extra={"provider": "console", "to": to})
        return True
