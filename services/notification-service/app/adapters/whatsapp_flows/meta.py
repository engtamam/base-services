"""Meta Flow management — Graph Flows API on the WhatsApp Business Account.

create  → POST /{waba_id}/flows       {name, categories, flow_json | clone_flow_id, endpoint_uri?,
                                       publish?}  → {id, success, validation_errors}
publish → POST /{flow_id}/publish     {} → {success}
list    → GET  /{waba_id}/flows       → {data:[{id, name, status, categories}, …]}

Graph base + WABA id are config/credential, never request input. A network/HTTP failure is a
not-provisioned outcome carrying a reason, never a crash — the core provisioner decides what to do
with it (and, on a failed list, does NOT blindly create).

Verified against Meta's WhatsApp Flows API reference (Sep 2026).
"""
from __future__ import annotations

import json

from app.adapters.notify_http import bearer, get_json, ok, post_json
from app.ports.errors import ConfigError
from app.ports.whatsapp_flow_manager import DRAFT, FlowResult, FlowSummary


def _parse(raw: str) -> dict:
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def _reason(raw: str) -> str:
    err = _parse(raw).get("error")
    if isinstance(err, dict):
        return str(err.get("message", "") or err.get("error_user_msg", "") or "")
    return ""


def _validation_errors(data: dict) -> tuple[str, ...]:
    out: list[str] = []
    for ve in data.get("validation_errors") or []:
        if isinstance(ve, dict):
            out.append(str(ve.get("message") or ve.get("error") or ve))
        else:
            out.append(str(ve))
    return tuple(out)


class MetaFlowManager:
    driver = "meta"

    def __init__(self, access_token: str, waba_id: str, base_url: str) -> None:
        if not access_token or not waba_id:
            raise ConfigError(
                "WHATSAPP_ACCESS_TOKEN and WHATSAPP_BUSINESS_ACCOUNT_ID are required for meta flow "
                "management")
        self._base = base_url.rstrip("/")
        self._waba = waba_id
        self._headers = bearer(access_token)

    def list_flows(self) -> list[FlowSummary]:
        status, raw = get_json(f"{self._base}/{self._waba}/flows",
                               params={"fields": "id,name,status,categories"}, headers=self._headers)
        if not ok(status):
            return []
        rows = _parse(raw).get("data") or []
        out: list[FlowSummary] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            cats = tuple(str(c) for c in (r.get("categories") or []))
            out.append(FlowSummary(id=str(r.get("id", "")), name=str(r.get("name", "")),
                                   status=str(r.get("status", DRAFT)).upper(), categories=cats))
        return out

    def create(self, *, name, categories, flow_json="", clone_flow_id="", endpoint_uri="",
               publish=False) -> FlowResult:
        payload: dict = {"name": name, "categories": list(categories)}
        # A clone copies an existing Flow (same name, new id); otherwise the caller's flow_json defines
        # it. Exactly one source — the core provisioner guarantees that before we get here.
        if clone_flow_id:
            payload["clone_flow_id"] = clone_flow_id
        if flow_json:
            payload["flow_json"] = flow_json
        if endpoint_uri:
            payload["endpoint_uri"] = endpoint_uri
        if publish:
            payload["publish"] = True
        status, raw = post_json(f"{self._base}/{self._waba}/flows", payload, self._headers)
        data = _parse(raw)
        if not ok(status) or not data.get("id"):
            return FlowResult(ok=False, name=name, reason=_reason(raw) or f"HTTP {status}",
                              validation_errors=_validation_errors(data))
        return FlowResult(ok=True, flow_id=str(data["id"]), name=name,
                          status="PUBLISHED" if publish else DRAFT,
                          validation_errors=_validation_errors(data))

    def publish(self, *, flow_id) -> FlowResult:
        status, raw = post_json(f"{self._base}/{flow_id}/publish", {}, self._headers)
        if not ok(status):
            return FlowResult(ok=False, flow_id=flow_id, reason=_reason(raw) or f"HTTP {status}")
        return FlowResult(ok=True, flow_id=flow_id, status="PUBLISHED")
