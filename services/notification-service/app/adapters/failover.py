"""Failover channels — try each provider in order until one succeeds.

`driver` reports the chain (e.g. "resend,console") for the manifest. Records
nothing here; the NotificationService records the delivery + which provider won.
"""
from __future__ import annotations

from app.observability.logging import get_logger

_log = get_logger("app.failover")


class FailoverEmail:
    def __init__(self, providers: list) -> None:
        self._providers = providers
        self.driver = ",".join(p.driver for p in providers)

    def send(self, to: str, subject: str, html: str, text: str, sender: str) -> tuple[bool, str]:
        for p in self._providers:
            try:
                if p.send(to, subject, html, text, sender):
                    return True, p.driver
            except Exception:
                _log.warning("email_provider_error", extra={"provider": p.driver})
        return False, self.driver


class FailoverSms:
    def __init__(self, providers: list) -> None:
        self._providers = providers
        self.driver = ",".join(p.driver for p in providers)

    def send(self, to: str, message: str) -> tuple[bool, str]:
        for p in self._providers:
            try:
                if p.send(to, message):
                    return True, p.driver
            except Exception:
                _log.warning("sms_provider_error", extra={"provider": p.driver})
        return False, self.driver
