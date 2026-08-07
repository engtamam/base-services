"""Meta template management — Graph message_templates on the WhatsApp Business Account.

submit → POST /{waba_id}/message_templates (name, language, category, components). Meta returns an
id and a review status (usually PENDING). status → GET the template and read its `status`
(APPROVED / REJECTED, with a reason). Graph base + WABA id are config, never a request.

Content-agnostic: `body` and `variables` are the adopter's; we shape them into Meta's component
format and forward. A network/HTTP failure is a not-yet-approved outcome, never a crash — the
template simply stays pending and is retried on the next sync.
"""
from __future__ import annotations

import json
import logging

from app.adapters.notify_http import bearer, ok, post_json
from app.ports.errors import ConfigError
from app.ports.whatsapp_template_manager import StatusResult, SubmitResult

_log = logging.getLogger("app.whatsapp_templates.meta")

_META_TO_STATUS = {"APPROVED": "approved", "REJECTED": "rejected", "PENDING": "pending",
                   "IN_APPEAL": "pending", "PENDING_DELETION": "pending"}


class MetaTemplateManager:
    driver = "meta"

    def __init__(self, access_token: str, waba_id: str, base_url: str) -> None:
        if not access_token or not waba_id:
            raise ConfigError(
                "WHATSAPP_ACCESS_TOKEN and WHATSAPP_BUSINESS_ACCOUNT_ID are required for meta "
                "template management")
        self._base = base_url.rstrip("/")
        self._waba = waba_id
        self._headers = bearer(access_token)

    def submit(self, *, name, language, category, body, variables) -> SubmitResult:
        components = [{"type": "BODY", "text": body}] if body else []
        payload = {"name": name, "language": language, "category": category,
                   "components": components}
        status, raw = post_json(f"{self._base}/{self._waba}/message_templates", payload,
                                self._headers)
        if not ok(status):
            # Not approved-yet, not a crash. Report pending so the row is retried, or rejected if
            # Meta clearly refused (4xx with an error). The reason is kept for the operator.
            reason = _reason(raw)
            return SubmitResult(ok=False, status="rejected" if 400 <= status < 500 else "pending",
                                reason=reason or f"HTTP {status}")
        data = _parse(raw)
        return SubmitResult(ok=True, provider_template_id=str(data.get("id", "")),
                            status=_META_TO_STATUS.get(str(data.get("status", "PENDING")).upper(),
                                                       "pending"))

    def status(self, *, provider_template_id, name, language) -> StatusResult:
        # Query by name on the WABA; a single template's status is what we need.
        import urllib.parse
        q = urllib.parse.urlencode({"name": name})
        status, raw = post_json(f"{self._base}/{self._waba}/message_templates?{q}", {},
                                {**self._headers, "X-HTTP-Method-Override": "GET"})
        if not ok(status):
            return StatusResult(status="pending", reason=f"HTTP {status}")
        data = _parse(raw)
        rows = data.get("data") or []
        for row in rows:
            if str(row.get("language")) == language or not language:
                meta_status = str(row.get("status", "PENDING")).upper()
                return StatusResult(status=_META_TO_STATUS.get(meta_status, "pending"),
                                    reason=str(row.get("rejected_reason", "") or ""))
        return StatusResult(status="pending")


def _parse(raw: str) -> dict:
    try:
        d = json.loads(raw or "{}")
        return d if isinstance(d, dict) else {}
    except (ValueError, TypeError):
        return {}


def _reason(raw: str) -> str:
    return str((_parse(raw).get("error") or {}).get("message", ""))
