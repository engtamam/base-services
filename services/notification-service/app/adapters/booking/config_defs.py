"""Definition provider from config — the single-brand booking flow, parsed once from JSON.

Per-tenant, hot-editable definitions (from runtime config or a panel) come with provisioning; for now
a deployment sets WHATSAPP_BOOKING_DEFINITION and every tenant on this instance shares it. Returns
None when unset or invalid, so the engine cleanly reports "not configured" rather than crashing."""
from __future__ import annotations

import json

from app.core.booking_flow import BookingError, FlowDefinition


def parse_definition(raw: str) -> FlowDefinition | None:
    if not raw or not raw.strip():
        return None
    try:
        d = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict):
        return None
    try:
        return FlowDefinition(
            steps=tuple(d.get("steps", ())),
            horizon_days=int(d.get("horizon_days", 30)),
            mode=str(d.get("mode", "single")),
            min_days=int(d.get("min_days", 1)),
            max_days=int(d.get("max_days", 14)),
            default_working_days=tuple(d.get("default_working_days", ())),
            strings=dict(d.get("strings", {})))
    except (BookingError, TypeError, ValueError):
        return None


class ConfigDefinitionProvider:
    """Holds one parsed definition (or None) and hands it to every tenant."""

    def __init__(self, raw: str) -> None:
        self._defn = parse_definition(raw)

    async def get(self, tenant: str) -> FlowDefinition | None:
        return self._defn
