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


class FailoverWhatsApp:
    def __init__(self, providers: list) -> None:
        self._providers = providers
        self.driver = ",".join(p.driver for p in providers)

    def send(self, to: str, template_name: str, language: str, variables: list,
             biz_opaque_callback_data: str = "", flow_token: str = "",
             flow_action_data: dict | None = None, header_media_id: str = "",
             button_payload: str = "", url_button_param: str = "") -> tuple[bool, str, str]:
        """Try each provider; return (ok, winning driver, provider_message_id). The message id is the
        wamid the receipt will echo — carried up so the delivery can be matched to its receipt."""
        for p in self._providers:
            try:
                mid = p.send(to, template_name, language, variables, biz_opaque_callback_data,
                             flow_token, flow_action_data, header_media_id, button_payload, url_button_param)
                if mid:                       # non-empty == success (and carries the wamid)
                    return True, p.driver, mid
            except Exception:
                _log.warning("whatsapp_provider_error", extra={"provider": p.driver})
        return False, self.driver, ""

    def send_interactive(self, to: str, interactive: dict,
                         biz_opaque_callback_data: str = "") -> tuple[bool, str, str]:
        """Same failover contract as send(), for an interactive (button/list) message."""
        for p in self._providers:
            try:
                mid = p.send_interactive(to, interactive, biz_opaque_callback_data)
                if mid:
                    return True, p.driver, mid
            except Exception:
                _log.warning("whatsapp_provider_error", extra={"provider": p.driver})
        return False, self.driver, ""

    def send_template_components(self, to: str, template_name: str, language: str,
                                 components: list, biz_opaque_callback_data: str = "") -> tuple[bool, str, str]:
        """Same failover contract as send(), for a template with a pre-built components list (carousel)."""
        for p in self._providers:
            try:
                mid = p.send_template_components(to, template_name, language, components,
                                                biz_opaque_callback_data)
                if mid:
                    return True, p.driver, mid
            except Exception:
                _log.warning("whatsapp_provider_error", extra={"provider": p.driver})
        return False, self.driver, ""

    def send_text(self, to: str, body: str,
                  biz_opaque_callback_data: str = "") -> tuple[bool, str, str]:
        """Same failover contract as send(), for a plain free-text (24h-window) reply."""
        for p in self._providers:
            try:
                mid = p.send_text(to, body, biz_opaque_callback_data)
                if mid:
                    return True, p.driver, mid
            except Exception:
                _log.warning("whatsapp_provider_error", extra={"provider": p.driver})
        return False, self.driver, ""
