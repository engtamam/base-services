"""Console flow manager — provisions in-memory for dev. No Meta round-trip.

Lets the whole Flow lifecycle run with zero infra: ensure a flow by name → it is "created" and
"published" at once, and a second ensure with the same name finds it (idempotent), exactly as the
meta driver behaves against a real WABA. The meta driver is the real, WABA-scoped provisioning.
"""
from __future__ import annotations

from app.observability.logging import get_logger
from app.ports.whatsapp_flow_manager import PUBLISHED, FlowResult, FlowSummary

_log = get_logger("app.whatsapp_flows.console")


class ConsoleFlowManager:
    driver = "console"

    def __init__(self) -> None:
        # Name → id, so a repeat ensure of the same name is idempotent within a process, mirroring
        # the meta driver reading an existing Flow back from the WABA.
        self._by_name: dict[str, str] = {}

    def list_flows(self) -> list[FlowSummary]:
        return [FlowSummary(id=fid, name=name, status=PUBLISHED)
                for name, fid in self._by_name.items()]

    def create(self, *, name, categories, flow_json="", clone_flow_id="", endpoint_uri="",
               publish=False) -> FlowResult:
        fid = self._by_name.get(name) or f"console-flow:{name}"
        self._by_name[name] = fid
        _log.info("whatsapp_flow_provisioned", extra={"provider": "console", "flow_name": name})
        return FlowResult(ok=True, flow_id=fid, name=name,
                          status=PUBLISHED if publish else "DRAFT")

    def publish(self, *, flow_id) -> FlowResult:
        return FlowResult(ok=True, flow_id=flow_id, status=PUBLISHED)

    def set_encryption_public_key(self, *, phone_number_id, public_key_pem) -> FlowResult:
        # Dev: no Meta round-trip. Auto-succeeds so the whole connect flow runs with zero infra.
        _log.info("whatsapp_flow_encryption_set", extra={"provider": "console",
                                                          "phone_number_id": phone_number_id})
        return FlowResult(ok=True, status=PUBLISHED)

    def encryption_public_key_status(self, *, phone_number_id) -> str:
        return "VALID"
