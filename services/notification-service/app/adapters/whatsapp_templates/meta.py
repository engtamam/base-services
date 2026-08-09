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

# Meta's status vocabulary → ours. It has grown over time (IN_REVIEW is the newer PENDING,
# APPEAL_REQUESTED the newer IN_APPEAL), so both spellings map. PAUSED and DISABLED are kept
# DISTINCT rather than folded into pending: a paused template is not under review (it can come back
# after the feedback that paused it subsides) and a disabled one never returns — neither is a
# "still being reviewed" the way the old default made them read.
_META_TO_STATUS = {"APPROVED": "approved", "REJECTED": "rejected",
                   "PENDING": "pending", "IN_REVIEW": "pending",
                   "IN_APPEAL": "pending", "APPEAL_REQUESTED": "pending",
                   "PENDING_DELETION": "pending",
                   "PAUSED": "paused", "DISABLED": "disabled"}


def _body_example(variables: list[str], examples: list[str]) -> list[str]:
    """One example VALUE per parameter, in order — Meta refuses a parameterised template without it.

    The count must equal the number of placeholders (== len(variables)). Where the caller gave a
    real example we use it; where it did not, we fall back to the variable NAME for that slot so the
    submission is not a hard 400. A name is a weak example, but a submittable one, and it keeps the
    template's name — which cannot be reused after approval — from being burned on a 400."""
    n = len(variables or [])
    if n == 0:
        return []
    vals = list(examples or [])
    out: list[str] = []
    for i in range(n):
        val = str(vals[i]).strip() if i < len(vals) and str(vals[i]).strip() else str(variables[i])
        out.append(val)
    return out


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

    def submit(self, *, name, language, category, body, variables, examples=None) -> SubmitResult:
        components = []
        if body:
            comp = {"type": "BODY", "text": body}
            ex = _body_example(variables, examples)
            if ex:
                # Meta's positional example shape: example.body_text is a LIST of example SETS, each
                # set one value per {{n}}. One set is enough for a single-language body.
                comp["example"] = {"body_text": [ex]}
            components.append(comp)
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
