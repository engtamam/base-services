"""WhatsApp Flow provisioning — idempotent-by-name ensure over a WABA.

The one operation an adopter's connect step needs: "make sure this shop's WABA has a PUBLISHED Flow
called <name>, creating (or cloning) and publishing it if it does not." A shop reconnects, a
background task retries — this must be safe to run repeatedly, because a published Flow is IMMUTABLE
and cannot be deleted, so a second blind create would litter the WABA with duplicates and leave the
templates' flow_NAME ambiguous.

So ensure() is list-first: find the Flow by name on the WABA and return it if already there (with an
extra publish only when it is still a draft); create or clone + publish only when it is genuinely
absent. Meta is the source of truth — no local mirror to drift.

Content-agnostic: the flow_json (or a clone_flow_id pointing at the adopter's master Flow) is passed
IN; this layer authors nothing. It validates only what Meta would reject anyway — a name, at least
one category from Meta's fixed set, and exactly one source (flow_json XOR clone_flow_id).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.ports.whatsapp_flow_manager import (DRAFT, FLOW_CATEGORIES, PUBLISHED, FlowResult)


class FlowError(ValueError):
    """A flow request the provisioner refuses before calling Meta (bad name/category/source)."""


@dataclass(frozen=True)
class EnsureResult:
    ok: bool
    flow_id: str
    name: str
    status: str
    created: bool          # True when this call created/cloned the Flow (vs found it already there)
    published: bool        # True when the Flow is PUBLISHED after this call
    reason: str = ""
    validation_errors: tuple[str, ...] = ()

    def public(self) -> dict[str, Any]:
        return {"ok": self.ok, "flow_id": self.flow_id, "name": self.name, "status": self.status,
                "created": self.created, "published": self.published, "reason": self.reason,
                "validation_errors": list(self.validation_errors)}


def _validate(name: str, categories: list[str], flow_json: str, clone_flow_id: str) -> list[str]:
    if not name or not name.strip():
        raise FlowError("a flow needs a name")
    cats = [str(c).strip().upper() for c in (categories or []) if str(c).strip()]
    if not cats:
        raise FlowError("a flow needs at least one category")
    bad = [c for c in cats if c not in FLOW_CATEGORIES]
    if bad:
        raise FlowError(f"categories must be from {FLOW_CATEGORIES}, got {bad}")
    # Exactly one source: author-from-json OR clone-from-master, never both, never neither. Cloning is
    # the multi-WABA path (one master authored once, a name-matched copy per shop); flow_json is the
    # create-from-scratch path.
    if bool(flow_json) == bool(clone_flow_id):
        raise FlowError("provide exactly one of flow_json or clone_flow_id")
    return cats


def ensure_flow(*, manager, name: str, categories: list[str], flow_json: str = "",
                clone_flow_id: str = "", endpoint_uri: str = "",
                publish: bool = True) -> EnsureResult:
    """Ensure a Flow named `name` exists (and is published) on the manager's WABA. Idempotent by name.

    `manager` is a WhatsAppFlowManager (meta in prod, console in dev). Its calls are blocking Graph
    I/O, so this is a SYNCHRONOUS orchestration unit: a caller on the event loop must dispatch the
    whole call via asyncio.to_thread (the route does), keeping the loop free while Meta is contacted."""
    cats = _validate(name, categories, flow_json, clone_flow_id)

    existing = next((f for f in manager.list_flows() if f.name == name), None)
    if existing is not None:
        # Already provisioned — the idempotent path. Publish only if the caller wants it and the
        # existing Flow is still a draft; a Flow already PUBLISHED (or deprecated/blocked) is left as
        # is rather than re-poked.
        if publish and existing.status == DRAFT:
            res = manager.publish(flow_id=existing.id)
            status = PUBLISHED if res.ok else existing.status
            return EnsureResult(ok=res.ok, flow_id=existing.id, name=name, status=status,
                                created=False, published=res.ok, reason=res.reason)
        return EnsureResult(ok=True, flow_id=existing.id, name=name, status=existing.status,
                            created=False, published=existing.status == PUBLISHED)

    created = manager.create(name=name, categories=cats, flow_json=flow_json,
                             clone_flow_id=clone_flow_id, endpoint_uri=endpoint_uri, publish=publish)
    if not created.ok:
        return EnsureResult(ok=False, flow_id="", name=name, status=DRAFT, created=False,
                            published=False, reason=created.reason,
                            validation_errors=created.validation_errors)
    # Some create paths publish in one call; when they do not, publish explicitly so the caller's
    # templates can reference a live Flow. A create that already reports PUBLISHED skips the extra call.
    published = created.status == PUBLISHED
    if publish and not published:
        pub = manager.publish(flow_id=created.flow_id)
        published = pub.ok
        status = PUBLISHED if pub.ok else DRAFT
        return EnsureResult(ok=pub.ok, flow_id=created.flow_id, name=name, status=status,
                            created=True, published=published, reason=pub.reason,
                            validation_errors=created.validation_errors)
    return EnsureResult(ok=True, flow_id=created.flow_id, name=name, status=created.status,
                        created=True, published=published,
                        validation_errors=created.validation_errors)
