"""Console WhatsApp provider — records the send to the log. Zero infra (default)."""
from __future__ import annotations

import uuid

from app.observability.logging import get_logger

_log = get_logger("app.whatsapp.console")


class ConsoleWhatsApp:
    driver = "console"

    def send(self, to: str, template_name: str, language: str, variables: list[str],
             biz_opaque_callback_data: str = "", flow_token: str = "",
             flow_action_data: dict | None = None, header_media_id: str = "",
             button_payload: str = "") -> str:
        _log.info("whatsapp_sent", extra={"provider": "console", "to": to,
                                          "template": template_name, "language": language,
                                          "header_media": bool(header_media_id),
                                          "button_payload": bool(button_payload)})
        # A synthetic id so the dev/console path exercises the same wamid→delivery plumbing as Meta.
        return f"console:{uuid.uuid4().hex}"

    def send_template_components(self, to: str, template_name: str, language: str,
                                 components: list[dict], biz_opaque_callback_data: str = "") -> str:
        _log.info("whatsapp_sent", extra={"provider": "console", "to": to,
                                          "template": template_name, "components": len(components)})
        return f"console:{uuid.uuid4().hex}"

    def send_interactive(self, to: str, interactive: dict,
                         biz_opaque_callback_data: str = "") -> str:
        _log.info("whatsapp_sent", extra={"provider": "console", "to": to,
                                          "interactive": interactive.get("type", "")})
        return f"console:{uuid.uuid4().hex}"

    def send_text(self, to: str, body: str,
                  biz_opaque_callback_data: str = "") -> str:
        _log.info("whatsapp_sent", extra={"provider": "console", "to": to, "type": "text"})
        return f"console:{uuid.uuid4().hex}"
