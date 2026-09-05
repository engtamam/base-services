"""Console template manager — auto-approves for dev. No Meta round-trip.

Lets the whole lifecycle run with zero infra: register -> submit (approved at once) -> send. The meta
driver is the real, human-gated review.
"""
from __future__ import annotations

from app.observability.logging import get_logger
from app.ports.whatsapp_template_manager import MessagingLimit, StatusResult, SubmitResult

_log = get_logger("app.whatsapp_templates.console")


class ConsoleTemplateManager:
    driver = "console"

    def submit(self, *, name, language, category, body, variables, examples=None,
               flow_button=None) -> SubmitResult:
        # `name` is a RESERVED LogRecord attribute — putting it in `extra` makes Python's logging
        # raise KeyError the moment the record is built (i.e. whenever this logger is at INFO), which
        # is how a console submit crashed in a real deployment while the tests, sitting above INFO,
        # stayed green. Use a non-reserved key.
        _log.info("whatsapp_template_submitted", extra={"provider": "console", "template_name": name})
        return SubmitResult(ok=True, provider_template_id=f"console:{name}:{language}",
                            status="approved")

    def status(self, *, provider_template_id, name, language) -> StatusResult:
        return StatusResult(status="approved")

    def messaging_limit(self, *, phone_number_id) -> MessagingLimit:
        # Dev has no Meta portfolio and no real ceiling — report unlimited/green so a limit-aware
        # caller (reserve, day plan) runs end to end without infra, exactly like submit auto-approves.
        return MessagingLimit(ok=True, tier="TIER_UNLIMITED", limit=-1, quality_rating="GREEN")
