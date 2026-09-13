"""WhatsApp approved-template registry — generic, content-agnostic.

Meta requires business-initiated WhatsApp messages to use a template it has APPROVED. This service
manages the LIFECYCLE of those templates — register, submit, track status, substitute variables at
send — but holds NO template semantics: `body` is opaque, and the adopter owns the copy. All this
layer knows is the template's identity (`tenant, name, language`), its Meta `category`, its ordered
`variables`, and its approval `status`.

Storage is tenant-scoped, filtered reads only (no scan). A template is added at RUNTIME through the
operator surface — the admin panel registers one, no redeploy.
"""
from __future__ import annotations

import datetime as _dt
import uuid
from dataclasses import dataclass, field
from typing import Any

# Meta's fixed set. A category outside it is refused rather than sent and rejected upstream.
CATEGORIES = ("UTILITY", "MARKETING", "AUTHENTICATION")

PENDING, APPROVED, REJECTED = "pending", "approved", "rejected"
# Distinct from rejected: Meta pauses a template for recurring negative feedback / low read-rates
# (it can return) and disables one that never does. An operator's next action differs, so the
# registry keeps them apart rather than folding either into pending or rejected.
PAUSED, DISABLED = "paused", "disabled"
# The statuses that carry a reason worth keeping (why it is not sendable).
_REASONED = (REJECTED, PAUSED, DISABLED)


class TemplateError(ValueError):
    """A template definition the registry refuses (bad category/variables/duplicate)."""


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _key(tenant: str, name: str, language: str) -> str:
    # Identity is (tenant, name, language): the same template exists once per language.
    return f"{tenant}|{name}|{language}"


@dataclass
class WhatsAppTemplate:
    id: str
    tenant: str
    name: str
    language: str
    category: str
    body: str = ""                       # opaque to this service; the adopter owns it
    variables: list[str] = field(default_factory=list)   # ordered parameter NAMES (send-time keys)
    examples: list[str] = field(default_factory=list)    # ordered example VALUES Meta needs at review
    status: str = PENDING
    provider_template_id: str = ""       # Meta's id, set on submit
    rejection_reason: str = ""
    # Optional Flow BUTTON declared as part of THIS template (approved by Meta at creation). Empty for
    # an ordinary template. Shape: {"flow_id"|"flow_name", "flow_action", "navigate_screen", "text"}.
    # The button + its flow must be approved before a send can carry the per-send flow action.
    flow_button: dict[str, Any] = field(default_factory=dict)
    # Optional CAROUSEL — a horizontally-swipeable strip of 2–10 cards, each with a media header and
    # its own buttons. Empty for an ordinary template. Shape: {"cards": [<card>, …]} where a card is
    # {"header_format": "IMAGE"|"VIDEO", "header_example_handle", "body"?, "body_examples"?,
    # "buttons": [{"type":"QUICK_REPLY","text"} | {"type":"URL","text","url","url_example"?}]}. All
    # cards must share the same component shape (Meta's rule); the send supplies each card's media +
    # variable values. Reviewed as one template — the whole carousel is approved together.
    carousel: dict[str, Any] = field(default_factory=dict)
    # Optional FOOTER — a short, fixed grey line under the message. Text-only: Meta allows NO
    # variables and caps it at 60 chars (verified against Meta's template-components docs, Sep 2026).
    # Empty for a template with no footer.
    footer: str = ""
    created: str = field(default_factory=_now)
    updated: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "tenant": self.tenant, "name": self.name, "language": self.language,
                "category": self.category, "body": self.body, "variables": self.variables,
                "examples": self.examples, "flow_button": self.flow_button,
                "carousel": self.carousel, "footer": self.footer,
                "status": self.status, "provider_template_id": self.provider_template_id,
                "rejection_reason": self.rejection_reason, "created": self.created,
                "updated": self.updated}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WhatsAppTemplate":
        return cls(id=d["id"], tenant=d.get("tenant", ""), name=d.get("name", ""),
                   language=d.get("language", ""), category=d.get("category", ""),
                   body=d.get("body", ""), variables=list(d.get("variables") or []),
                   examples=list(d.get("examples") or []),
                   flow_button=dict(d.get("flow_button") or {}),
                   carousel=dict(d.get("carousel") or {}),
                   footer=str(d.get("footer") or ""),
                   status=d.get("status", PENDING),
                   provider_template_id=d.get("provider_template_id", ""),
                   rejection_reason=d.get("rejection_reason", ""),
                   created=d.get("created", _now()), updated=d.get("updated", _now()))

    def public(self) -> dict[str, Any]:
        return self.to_dict()


# Meta caps a template FOOTER at 60 characters and allows no variables in it (verified against
# Meta's template-components docs, Sep 2026). Enforced here so a bad footer is a clean 400 from us
# rather than a burned template name at Meta.
_FOOTER_MAX = 60


def _validate_footer(footer: str) -> str:
    """Normalise + validate a footer, or raise TemplateError. Returns "" for no footer."""
    f = str(footer or "").strip()
    if not f:
        return ""
    if "{{" in f or "}}" in f:
        raise TemplateError("a footer is text-only — Meta allows no variables ({{…}}) in a footer")
    if len(f) > _FOOTER_MAX:
        raise TemplateError(f"a footer is at most {_FOOTER_MAX} characters ({len(f)} given)")
    return f


_CAROUSEL_MIN, _CAROUSEL_MAX = 2, 10
_CARD_HEADER_FORMATS = ("IMAGE", "VIDEO")
_CARD_BUTTON_TYPES = ("QUICK_REPLY", "URL")


def _card_shape(card: dict[str, Any]) -> tuple:
    """The structural signature Meta requires to be IDENTICAL across every card: header format, whether
    a body is present, and the ordered button (type, text) pairs. Values (the media, the variables)
    differ per card and per send; the SHAPE may not."""
    buttons = tuple((str(b.get("type", "")).upper(), str(b.get("text", "")))
                    for b in (card.get("buttons") or []))
    return (str(card.get("header_format", "")).upper(), bool(card.get("body")), buttons)


def _validate_carousel(carousel: dict[str, Any]) -> dict[str, Any]:
    """Normalise + validate a carousel spec, or raise TemplateError. Returns {} for no carousel.

    Enforces Meta's own creation rules locally so a bad spec is a clear error, not an opaque 400 that
    burns the template name: 2–10 cards; every card the SAME component shape (same header format,
    same body-or-not, same ordered buttons); each card a media header with an example handle Meta
    needs at review; each button a valid type with the fields that type requires."""
    cards = list(carousel.get("cards") or [])
    if not cards:
        return {}
    if not (_CAROUSEL_MIN <= len(cards) <= _CAROUSEL_MAX):
        raise TemplateError(f"a carousel needs {_CAROUSEL_MIN}–{_CAROUSEL_MAX} cards ({len(cards)} given)")
    shape0 = _card_shape(cards[0])
    out_cards: list[dict[str, Any]] = []
    for i, card in enumerate(cards):
        if _card_shape(card) != shape0:
            raise TemplateError("all carousel cards must have the same component shape "
                                "(header format, body presence, and buttons)")
        fmt = str(card.get("header_format", "")).upper()
        if fmt not in _CARD_HEADER_FORMATS:
            raise TemplateError(f"card {i}: header_format must be one of {_CARD_HEADER_FORMATS}")
        if not card.get("header_example_handle"):
            raise TemplateError(f"card {i}: a header_example_handle is required at creation "
                                "(upload the media first to get a resumable-upload handle)")
        buttons = list(card.get("buttons") or [])
        if not buttons:
            raise TemplateError(f"card {i}: a carousel card needs at least one button")
        norm_buttons: list[dict[str, Any]] = []
        for b in buttons:
            bt = str(b.get("type", "")).upper()
            if bt not in _CARD_BUTTON_TYPES:
                raise TemplateError(f"card {i}: button type must be one of {_CARD_BUTTON_TYPES}")
            if not b.get("text"):
                raise TemplateError(f"card {i}: each button needs text")
            nb: dict[str, Any] = {"type": bt, "text": str(b["text"])}
            if bt == "URL":
                if not b.get("url"):
                    raise TemplateError(f"card {i}: a URL button needs a url")
                nb["url"] = str(b["url"])
                if b.get("url_example"):
                    nb["url_example"] = [str(e) for e in b["url_example"]]
            norm_buttons.append(nb)
        nc: dict[str, Any] = {"header_format": fmt,
                              "header_example_handle": str(card["header_example_handle"]),
                              "buttons": norm_buttons}
        if card.get("body"):
            nc["body"] = str(card["body"])
            if card.get("body_examples"):
                nc["body_examples"] = [str(e) for e in card["body_examples"]]
        out_cards.append(nc)
    return {"cards": out_cards}


class WhatsAppTemplateStore:
    collection = "whatsapp_templates"

    def __init__(self, store) -> None:
        self._s = store

    async def save(self, t: WhatsAppTemplate) -> WhatsAppTemplate:
        await self._s.put(self.collection, _key(t.tenant, t.name, t.language), t.to_dict())
        return t

    async def get(self, tenant: str, name: str, language: str) -> WhatsAppTemplate | None:
        row = await self._s.get(self.collection, _key(tenant, name, language))
        return WhatsAppTemplate.from_dict(row) if row else None

    async def find(self, *, tenant: str, status: str = "", limit: int = 100,
                   after: str | None = None) -> tuple[list[dict], str | None]:
        if not tenant:
            raise TemplateError("find requires a tenant — an empty scope is every customer")
        where: dict[str, Any] = {"tenant": tenant}
        if status:
            where["status"] = status
        return await self._s.find(self.collection, where, limit=limit, after=after,
                                  sort=("created", "desc"))


class WhatsAppTemplateRegistry:
    """Register / list / get. Submission + status live in the template manager (54.3)."""

    def __init__(self, store: WhatsAppTemplateStore) -> None:
        self._store = store

    async def register(self, *, tenant: str, name: str, language: str, category: str,
                       body: str = "", variables: list[str] | None = None,
                       examples: list[str] | None = None,
                       flow_button: dict[str, Any] | None = None,
                       carousel: dict[str, Any] | None = None,
                       footer: str = "") -> WhatsAppTemplate:
        if not tenant or not name or not language:
            raise TemplateError("tenant, name and language are required")
        cat = (category or "").strip().upper()
        if cat not in CATEGORIES:
            raise TemplateError(f"category must be one of {CATEGORIES}, got {category!r}")
        vs = [str(v) for v in (variables or [])]
        exs = [str(e) for e in (examples or [])]
        fb = dict(flow_button or {})
        if fb and not (fb.get("flow_id") or fb.get("flow_name")):
            raise TemplateError("a flow_button needs flow_id or flow_name")
        car = _validate_carousel(dict(carousel or {}))
        ftr = _validate_footer(footer)
        existing = await self._store.get(tenant, name, language)
        # register() is an upsert, and it is the natural home of "make sure this tenant has the
        # library" — run over the whole set whenever one template is added. So it must NOT move an
        # unchanged template back to pending: that would un-approve every other template in the
        # account and stop every business-initiated message until each was re-reviewed. Reset to
        # pending ONLY when the reviewable content actually changed; Meta re-opens review on an edit,
        # not on a no-op re-register.
        unchanged = (existing is not None and existing.body == body and existing.variables == vs
                     and existing.examples == exs and existing.category == cat
                     and existing.flow_button == fb and existing.carousel == car
                     and existing.footer == ftr)
        status = existing.status if unchanged else PENDING
        t = WhatsAppTemplate(
            id=existing.id if existing else uuid.uuid4().hex,
            tenant=tenant, name=name, language=language, category=cat, body=body, variables=vs,
            examples=exs, flow_button=fb, carousel=car, footer=ftr, status=status,
            provider_template_id=existing.provider_template_id if existing else "",
            rejection_reason=existing.rejection_reason if (unchanged and existing) else "",
            created=existing.created if existing else _now())
        t.updated = _now()
        return await self._store.save(t)

    async def get(self, tenant: str, name: str, language: str) -> WhatsAppTemplate | None:
        return await self._store.get(tenant, name, language)

    async def list(self, tenant: str, *, status: str = "", limit: int = 100,
                   after: str | None = None) -> tuple[list[dict], str | None]:
        return await self._store.find(tenant=tenant, status=status, limit=limit, after=after)

    # ── lifecycle (54.3): submit to Meta + reconcile status ─────────────────────
    async def submit(self, *, tenant: str, name: str, language: str, manager) -> WhatsAppTemplate:
        """Hand a registered template to Meta for review; record its id + returned status."""
        t = await self._store.get(tenant, name, language)
        if t is None:
            raise TemplateError("no such template to submit")
        res = manager.submit(name=t.name, language=t.language, category=t.category, body=t.body,
                             variables=t.variables, examples=t.examples, flow_button=t.flow_button,
                             carousel=t.carousel, footer=t.footer)
        if res.provider_template_id:
            t.provider_template_id = res.provider_template_id
        t.status = res.status or PENDING
        t.rejection_reason = res.reason if res.status in _REASONED else ""
        t.updated = _now()
        return await self._store.save(t)

    async def submit_pending(self, *, tenant: str, manager, limit: int = 100,
                             scan_limit: int = 200) -> dict[str, Any]:
        """Submit the tenant's NEVER-SUBMITTED templates to Meta, up to `limit`, oldest first.

        This is the backend answer to "upload the whole library on connect": Meta rate-limits template
        creation to 100/hour per WABA, and a library is often larger, so a scheduled tick drains the
        next batch until nothing is left — the throttle lives HERE, where Meta's limit is known,
        instead of in every caller. `limit` is clamped to Meta's ceiling so no config can exceed it.

        IDEMPOTENT by construction: the drain targets pending templates that have NO
        provider_template_id — i.e. registered but not yet handed to Meta. A template already
        submitted (awaiting a verdict) carries its id and is skipped, so a re-run never double-submits.
        One template's failure (a Meta hiccup, a bad definition) records its reason and does not stop
        the batch — the same one-bad-row-must-not-stop-the-sweep discipline as sync/cycle.
        """
        limit = max(0, min(int(limit), 100))     # Meta's hard 100/hour ceiling; never exceeded
        rows, _ = await self._store.find(tenant=tenant, status=PENDING, limit=max(limit, scan_limit))
        # Never handed to Meta yet == no provider id. Oldest first, so the library uploads in order
        # and a stuck tail cannot starve earlier templates across ticks.
        unsent = sorted((r for r in rows if not r.get("provider_template_id")),
                        key=lambda r: r.get("created", ""))
        submitted = failed = 0
        errors: list[dict[str, str]] = []
        for row in unsent[:limit]:
            t = WhatsAppTemplate.from_dict(row)
            try:
                await self.submit(tenant=tenant, name=t.name, language=t.language, manager=manager)
                submitted += 1
            except Exception as exc:   # noqa: BLE001 — one unsubmittable template must not stop the batch
                failed += 1
                errors.append({"name": t.name, "language": t.language, "error": type(exc).__name__})
        return {"tenant": tenant, "pending_unsent": len(unsent), "submitted": submitted,
                "failed": failed, "remaining": max(0, len(unsent) - submitted), "errors": errors}

    # The statuses worth re-checking against Meta. rejected and disabled are terminal (a rejected
    # name cannot be resubmitted; a disabled template does not return), so re-querying them is wasted
    # calls. Everything else is LIVE: pending awaits a verdict, paused can recover to approved, and —
    # the case that matters most — an APPROVED template can be paused or disabled by Meta for negative
    # feedback, which must be caught or the product keeps sending into a template Meta has muted.
    _LIVE = (PENDING, APPROVED, PAUSED)

    async def sync(self, *, tenant: str, manager, limit: int = 200) -> dict[str, int]:
        """Reconcile this tenant's live templates against Meta's verdict.

        Tenant-scoped filtered read (no scan). A status CHANGE is a durable fact written down, never
        a bus hint — the operator sees approved/rejected/paused/disabled without asking Meta again."""
        rows, _ = await self._store.find(tenant=tenant, limit=limit)
        live = [r for r in rows if r.get("status", PENDING) in self._LIVE]
        updated = 0
        for row in live:
            t = WhatsAppTemplate.from_dict(row)
            res = manager.status(provider_template_id=t.provider_template_id, name=t.name,
                                 language=t.language)
            if res.status and res.status != t.status:
                t.status = res.status
                t.rejection_reason = res.reason if res.status in _REASONED else ""
                t.updated = _now()
                await self._store.save(t)
                updated += 1
        return {"scanned": len(live), "updated": updated}
