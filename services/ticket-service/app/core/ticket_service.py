"""TicketService — the engine. Every mutation appends to the log and updates the projection.

The state machine is ENFORCED here, in the core, not trusted from the client: an illegal transition
raises `InvalidTransition`, a caller without the transition's capability raises `TransitionForbidden`,
and opening a type without its required fields raises `RequiredFieldMissing`. Enforcing in the
service (not only the route) means the rule holds no matter which caller reaches it, and it is
testable without HTTP.

Notifications and AI suggestions are OPTIONAL side-hooks (later tasks). They are best-effort and
never block or fail a ticket operation — the durable fact is the event log; a hook is a courtesy.
"""
from __future__ import annotations

from typing import Any

from app.core.models import PRIORITIES, Ticket, new_id, now
from app.core.pii import subject_ref
from app.core.workflow import WorkflowError, WorkflowRegistry


class TicketError(Exception):
    """Base for domain refusals the API maps to 4xx."""


class UnknownType(TicketError):
    """The ticket type is not configured."""


class RequiredFieldMissing(TicketError):
    def __init__(self, fields: list[str]) -> None:
        self.fields = fields
        super().__init__(f"missing required field(s): {', '.join(fields)}")


class InvalidTransition(TicketError):
    """A status move the type's workflow does not allow."""


class TransitionForbidden(TicketError):
    """The caller lacks the capability this transition requires."""


class TicketNotFound(TicketError):
    """No such ticket in this tenant (same answer as 'in another tenant' — never leak existence)."""


class TicketService:
    def __init__(self, *, tickets, events, workflow: WorkflowRegistry, pii_salt: str,
                 operator_cap: str = "tickets.manage", notifier=None, suggester=None) -> None:
        self._tickets = tickets
        self._events = events
        self._wf = workflow
        self._salt = pii_salt
        self._operator_cap = operator_cap
        self._notifier = notifier          # 53.5: durable outbox to notification-service
        self._suggester = suggester        # 53.7: advisory AI, off by default

    def pseudonym(self, tenant: str, subject: str) -> str:
        return subject_ref(self._salt, tenant, subject)

    # ── open ────────────────────────────────────────────────────────────────────
    async def open(self, *, tenant: str, type: str, subject: str, body: str, raiser: str,
                   ref: dict | None = None, priority: str = "normal",
                   metadata: dict | None = None) -> Ticket:
        try:
            tt = self._wf.require(type)
        except WorkflowError as exc:
            raise UnknownType(str(exc)) from exc
        present = {"subject": subject, "body": body, "ref": (ref or {}).get("id"),
                   "priority": priority}
        missing = [f for f in tt.required_fields if not present.get(f) and not (ref or {}).get(f)]
        if missing:
            raise RequiredFieldMissing(missing)
        if priority not in PRIORITIES:
            priority = "normal"
        raiser_ref = self.pseudonym(tenant, raiser)
        t = Ticket(id=new_id(), tenant=tenant, type=type, subject=subject, status=tt.start,
                   raiser_ref=raiser_ref, priority=priority, ref=dict(ref or {}),
                   meta=dict(metadata or {}))
        await self._tickets.save(t)
        await self._events.append(tenant=tenant, ticket_id=t.id, kind="created",
                                  actor_ref=raiser_ref, body=body,
                                  data={"type": type, "status": tt.start})
        await self._after(t, tt, "created", actor_ref=raiser_ref, body=body)
        return t

    # ── reply ─────────────────────────────────────────────────────────────────
    async def reply(self, *, tenant: str, ticket_id: str, author: str, body: str,
                    is_operator: bool = False) -> Ticket:
        t = await self._fetch(tenant, ticket_id)
        tt = self._wf.require(t.type)
        actor_ref = author if is_operator else self.pseudonym(tenant, author)
        await self._events.append(tenant=tenant, ticket_id=t.id, kind="replied",
                                  actor_ref=actor_ref, body=body,
                                  data={"by": "operator" if is_operator else "customer"})
        t.touch()
        await self._tickets.save(t)
        await self._after(t, tt, "replied", actor_ref=actor_ref, body=body)
        return t

    # ── transition ──────────────────────────────────────────────────────────────
    async def transition(self, *, tenant: str, ticket_id: str, to: str, actor: str,
                         actor_caps: set[str], is_operator: bool = False) -> Ticket:
        t = await self._fetch(tenant, ticket_id)
        tt = self._wf.require(t.type)
        if t.status == to:
            return t                        # idempotent: already there, no event, no notify
        if not tt.can_transition(t.status, to):
            raise InvalidTransition(f"{t.type}: cannot move {t.status!r} -> {to!r}")
        needed = tt.capability_for(to, self._operator_cap)
        if needed not in actor_caps:
            raise TransitionForbidden(f"transition to {to!r} requires {needed!r}")
        actor_ref = actor if is_operator else self.pseudonym(tenant, actor)
        frm = t.status
        t.status = to
        t.touch()
        await self._tickets.save(t)
        # `resolved` marks a move to a TERMINAL status, so a KPI read (kpis.ticket_kpis) can derive
        # resolution time from the log without knowing this type's workflow — the log carries the fact.
        data = {"from": frm, "to": to, "resolved": to in tt.terminal}
        await self._events.append(tenant=tenant, ticket_id=t.id, kind="status_changed",
                                  actor_ref=actor_ref, data=data)
        await self._after(t, tt, "status_changed", actor_ref=actor_ref, data=data)
        return t

    # ── escalate ────────────────────────────────────────────────────────────────
    async def escalate(self, *, tenant: str, ticket_id: str, actor: str, level: str = "",
                        reason: str = "", is_operator: bool = True) -> Ticket:
        """Record that a ticket was ESCALATED — a first-class support fact, not a guess derived from a
        status name. It appends an `escalated` event (with an optional severity level) so 'was it
        escalated, when, how far' is answerable from the log for an SLA/stipend KPI. Idempotent-friendly
        by nature: each call records one escalation; a KPI keys on the FIRST."""
        t = await self._fetch(tenant, ticket_id)
        actor_ref = actor if is_operator else self.pseudonym(tenant, actor)
        t.touch()
        await self._tickets.save(t)
        await self._events.append(tenant=tenant, ticket_id=t.id, kind="escalated",
                                  actor_ref=actor_ref, body=reason, data={"level": level})
        await self._after(t, self._wf.require(t.type), "escalated", actor_ref=actor_ref)
        return t

    # ── assign / tag / rate ─────────────────────────────────────────────────────
    async def assign(self, *, tenant: str, ticket_id: str, assignee: str, actor: str) -> Ticket:
        t = await self._fetch(tenant, ticket_id)
        t.assignee = assignee
        t.touch()
        await self._tickets.save(t)
        await self._events.append(tenant=tenant, ticket_id=t.id, kind="assigned",
                                  actor_ref=actor, data={"assignee": assignee})
        return t

    async def tag(self, *, tenant: str, ticket_id: str, tags: list[str], actor: str) -> Ticket:
        t = await self._fetch(tenant, ticket_id)
        t.tags = sorted({*t.tags, *[s for s in tags if s]})
        t.touch()
        await self._tickets.save(t)
        await self._events.append(tenant=tenant, ticket_id=t.id, kind="tagged",
                                  actor_ref=actor, data={"tags": t.tags})
        return t

    async def rate(self, *, tenant: str, ticket_id: str, rater: str, score: int,
                   comment: str = "") -> Ticket:
        t = await self._fetch(tenant, ticket_id)
        score = max(1, min(5, int(score)))
        await self._events.append(tenant=tenant, ticket_id=t.id, kind="rated",
                                  actor_ref=self.pseudonym(tenant, rater), body=comment,
                                  data={"score": score})
        t.touch()
        await self._tickets.save(t)
        return t

    # ── AI advisory (operator, on demand) ──────────────────────────────────────
    async def suggest_reply(self, tenant: str, ticket_id: str) -> dict[str, Any]:
        """An assistant-drafted reply for an agent to edit and send. Advisory: it drafts, it never
        sends, and it returns supported=False when no AI is wired rather than faking an answer."""
        t = await self._fetch(tenant, ticket_id)
        if self._suggester is None:
            return {"supported": False}
        hist, _ = await self._events.history(tenant=tenant, ticket_id=ticket_id, limit=100)
        thread = [{"kind": e.get("kind"), "actor": e.get("actor_ref"), "body": e.get("body", "")}
                  for e in hist if e.get("kind") in ("created", "replied")]
        draft = await self._suggester.draft_reply(t, thread)
        if draft is None:
            return {"supported": False}
        return {"supported": True, **draft.to_dict()}

    # ── reads ─────────────────────────────────────────────────────────────────
    async def get(self, tenant: str, ticket_id: str) -> Ticket:
        return await self._fetch(tenant, ticket_id)

    async def history(self, tenant: str, ticket_id: str, *, limit: int = 200,
                      after: str | None = None) -> tuple[list[dict], str | None]:
        await self._fetch(tenant, ticket_id)   # authorizes the ticket belongs to this tenant
        return await self._events.history(tenant=tenant, ticket_id=ticket_id, limit=limit, after=after)

    # ── internals ───────────────────────────────────────────────────────────────
    async def _fetch(self, tenant: str, ticket_id: str) -> Ticket:
        t = await self._tickets.fetch(ticket_id)
        if t is None or t.tenant != tenant:
            # Never distinguish "wrong tenant" from "does not exist" — that would let one tenant
            # probe another's ticket ids (T6).
            raise TicketNotFound(ticket_id)
        return t

    async def _after(self, t: Ticket, tt, event: str, *, actor_ref: str = "", body: str = "",
                     data: dict | None = None) -> None:
        """Best-effort side-hooks: notify (53.5) and AI suggest (53.7). Never block the operation."""
        if self._notifier is not None and event in (tt.notify.get("on") or []):
            try:
                await self._notifier.on_ticket_event(t, tt, event, data=data or {})
            except Exception:  # noqa: BLE001 — a notification must never fail a ticket action
                pass
        if self._suggester is not None and event == "created":
            try:
                await self._suggester.on_created(t, body=body)
            except Exception:  # noqa: BLE001 — advisory only; never blocks
                pass
