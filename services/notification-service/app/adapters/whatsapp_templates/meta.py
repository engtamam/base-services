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
from app.ports.whatsapp_template_manager import MessagingLimit, StatusResult, SubmitResult

# The Graph field that returns a business phone number's current messaging limit. Meta DEPRECATED
# `messaging_limit_tier` in favour of this one — reading the old field would be a silently-wrong or
# empty answer, exactly the "one revision behind" trap. Requested alongside the quality rating and
# the number's display fields in a single GET.
_LIMIT_FIELDS = ("whatsapp_business_manager_messaging_limit,quality_rating,"
                 "display_phone_number,verified_name,name_status")


def _tier_to_int(tier: str) -> int:
    """Meta's tier code → an integer ceiling of unique customers / 24h. -1 unlimited, 0 unknown.

    Parsed generically (TIER_<n>[K] / TIER_UNLIMITED) rather than from a fixed table, so a tier Meta
    adds later still yields a number instead of a zero — the raw `tier` is preserved regardless."""
    t = (tier or "").strip().upper()
    if t.startswith("TIER_"):
        t = t[len("TIER_"):]
    if t in ("UNLIMITED", "UNLTD", "INF"):
        return -1
    mult = 1
    if t.endswith("K"):
        t, mult = t[:-1], 1000
    elif t.endswith("M"):
        t, mult = t[:-1], 1_000_000
    try:
        return int(float(t) * mult)
    except (TypeError, ValueError):
        return 0

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


def _flow_button_component(flow_button: dict | None) -> dict | None:
    """Meta's BUTTONS component carrying a single FLOW button, or None when no flow is declared.

    Verified against Meta's "Create a message template with a Flow" (Sep 2026): a BUTTONS component
    whose button is `{"type":"FLOW", "text", <flow identifier>, "flow_action", "navigate_screen"?}`.
    Only the text and ONE flow identifier (flow_id | flow_name | flow_json) are required; flow_action
    defaults to "navigate", and navigate_screen is required only for a navigate action — so it is sent
    only when both are present. The registry already guarantees an identifier before we get here."""
    fb = dict(flow_button or {})
    if not fb:
        return None
    button: dict = {"type": "FLOW", "text": str(fb.get("text") or "Open")}
    if fb.get("flow_id"):
        button["flow_id"] = str(fb["flow_id"])
    elif fb.get("flow_name"):
        button["flow_name"] = str(fb["flow_name"])
    elif fb.get("flow_json"):
        button["flow_json"] = fb["flow_json"]
    action = str(fb.get("flow_action") or "").strip()
    if action:
        button["flow_action"] = action
    # navigate_screen is meaningful (and required) only for the navigate action; omit it otherwise so
    # a data_exchange flow is not sent a screen id it must not carry.
    if fb.get("navigate_screen") and action != "data_exchange":
        button["navigate_screen"] = str(fb["navigate_screen"])
    return {"type": "BUTTONS", "buttons": [button]}


def _carousel_component(carousel: dict | None) -> dict | None:
    """Meta's CAROUSEL component from a validated carousel spec, or None when there is no carousel.

    Verified against Meta's carousel-template creation shape (Sep 2026): a {"type":"carousel","cards":
    [{"components":[…]}]} where each card carries a HEADER (format IMAGE/VIDEO + example.header_handle),
    an optional BODY (with example.body_text like the top-level body), and a BUTTONS component. The
    registry already validated the cards (2–10, uniform shape, an example handle each), so this only
    shapes them into Meta's component tree."""
    cards = list((carousel or {}).get("cards") or [])
    if not cards:
        return None
    out_cards: list[dict] = []
    for card in cards:
        comps: list[dict] = [{
            "type": "HEADER",
            "format": str(card.get("header_format", "")).upper(),
            "example": {"header_handle": [str(card.get("header_example_handle", ""))]},
        }]
        if card.get("body"):
            body_comp: dict = {"type": "BODY", "text": str(card["body"])}
            exs = [str(e) for e in (card.get("body_examples") or [])]
            if exs:
                body_comp["example"] = {"body_text": [exs]}
            comps.append(body_comp)
        buttons: list[dict] = []
        for b in card.get("buttons") or []:
            bt = str(b.get("type", "")).upper()
            btn: dict = {"type": bt, "text": str(b.get("text", ""))}
            if bt == "URL":
                btn["url"] = str(b.get("url", ""))
                if b.get("url_example"):
                    btn["example"] = [str(e) for e in b["url_example"]]
            buttons.append(btn)
        comps.append({"type": "BUTTONS", "buttons": buttons})
        out_cards.append({"components": comps})
    return {"type": "CAROUSEL", "cards": out_cards}


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

    def submit(self, *, name, language, category, body, variables, examples=None,
               flow_button=None, carousel=None) -> SubmitResult:
        components = []
        if body:
            comp = {"type": "BODY", "text": body}
            ex = _body_example(variables, examples)
            if ex:
                # Meta's positional example shape: example.body_text is a LIST of example SETS, each
                # set one value per {{n}}. One set is enough for a single-language body.
                comp["example"] = {"body_text": [ex]}
            components.append(comp)
        flow_comp = _flow_button_component(flow_button)
        if flow_comp:
            components.append(flow_comp)
        carousel_comp = _carousel_component(carousel)
        if carousel_comp:
            components.append(carousel_comp)
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

    def messaging_limit(self, *, phone_number_id) -> MessagingLimit:
        """GET /{phone_number_id}?fields=whatsapp_business_manager_messaging_limit,quality_rating,…

        The limit and quality live on the PHONE NUMBER (the WABA holds the templates). Read live —
        Meta can move the tier within about six hours of the conditions being met, so a value cached
        indefinitely would plan a campaign against a ceiling that no longer exists."""
        if not phone_number_id:
            return MessagingLimit(ok=False, error="no phone_number_id for this tenant")
        status, raw = post_json(f"{self._base}/{phone_number_id}?fields={_LIMIT_FIELDS}", {},
                                {**self._headers, "X-HTTP-Method-Override": "GET"})
        if not ok(status):
            return MessagingLimit(ok=False, error=_reason(raw) or f"HTTP {status}")
        data = _parse(raw)
        tier = str(data.get("whatsapp_business_manager_messaging_limit", "") or "")
        return MessagingLimit(
            ok=True, tier=tier, limit=_tier_to_int(tier),
            quality_rating=str(data.get("quality_rating", "") or ""),
            display_phone_number=str(data.get("display_phone_number", "") or ""),
            verified_name=str(data.get("verified_name", "") or ""),
            name_status=str(data.get("name_status", "") or ""))


def _parse(raw: str) -> dict:
    try:
        d = json.loads(raw or "{}")
        return d if isinstance(d, dict) else {}
    except (ValueError, TypeError):
        return {}


def _reason(raw: str) -> str:
    return str((_parse(raw).get("error") or {}).get("message", ""))
