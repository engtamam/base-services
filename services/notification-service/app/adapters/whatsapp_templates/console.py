"""Console template manager — auto-approves for dev. No Meta round-trip.

Lets the whole lifecycle run with zero infra: register -> submit (approved at once) -> send. The meta
driver is the real, human-gated review.
"""
from __future__ import annotations

from app.observability.logging import get_logger
from app.ports.whatsapp_template_manager import StatusResult, SubmitResult

_log = get_logger("app.whatsapp_templates.console")


class ConsoleTemplateManager:
    driver = "console"

    def submit(self, *, name, language, category, body, variables, examples=None) -> SubmitResult:
        _log.info("whatsapp_template_submitted", extra={"provider": "console", "name": name})
        return SubmitResult(ok=True, provider_template_id=f"console:{name}:{language}",
                            status="approved")

    def status(self, *, provider_template_id, name, language) -> StatusResult:
        return StatusResult(status="approved")
