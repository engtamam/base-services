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


class ReplyForbidden(TicketError):
    """The caller lacks the per-type reply capability this ticket type requires."""


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
    async def _mutate(self, tenant: str, ticket_id: str, apply, *, retries: int = 5) -> Ticket:
        """fetch -> apply -> cas_save, retrying on a lost race. The one lose-safe write path for the
        ticket row: assign/tag/escalate/rate used a plain save() that clobbered a concurrent CAS status
        transition (ticket-r2-03). `apply(ticket)` mutates in place and returns an event dict (kind +
        actor_ref + body/data) to append once the write wins, or None to append nothing."""
        for _ in range(max(1, retries)):
            t = await self._fetch(tenant, ticket_id)
            ev = apply(t)
            t.touch()
            if await self._tickets.cas_save(t):
                if ev:
                    await self._events.append(tenant=tenant, ticket_id=t.id, **ev)
                return t
        raise InvalidTransition(f"{ticket_id}: concurrent updates could not converge")

    async def reply(self, *, tenant: str, ticket_id: str, author: str, body: str,
                    is_operator: bool = False, by: str = "",
                    caps: set[str] | None = None) -> Ticket:
        t = await self._fetch(tenant, ticket_id)
        tt = self._wf.require(t.type)
        # Per-type reply capability enforced HERE, in the core, so every JWT-authenticated path is
        # judged by the same rule (ticket-r2-07) rather than only the user route. An operator is always
        # allowed. Callers that authenticate by a JWT pass their `caps`; the grantee path (authorized by
        # a writable consent grant) and the inbound path (the customer on their own ticket) pass
        # caps=None and are exempt by design — their authorization is the grant / customer identity.
        if caps is not None and not is_operator:
            needed = tt.reply_capability
            if needed and needed not in caps:
                raise ReplyForbidden(f"reply to a {t.type!r} ticket requires {needed!r}")
        # ALWAYS pseudonymize the actor into the immutable, hash-chained event log — operators included
        # (ticket-r2-02). The `by` label below carries the role for KPIs, so no raw identity is needed.
        actor_ref = self.pseudonym(tenant, author)
        # `by` labels the author class in the log ("operator"/"customer"/"grantee"). A KPI keys
        # first-response on "operator" only, so a grantee's reply is recorded truthfully as its own
        # class and never miscounted as the shop's own SLA response.
        label = by or ("operator" if is_operator else "customer")
        await self._events.append(tenant=tenant, ticket_id=t.id, kind="replied",
                                  actor_ref=actor_ref, body=body, data={"by": label})
        t.touch()
        await self._tickets.save(t)
        await self._after(t, tt, "replied", actor_ref=actor_ref, body=body)
        return t

    # ── transition ──────────────────────────────────────────────────────────────
    async def transition(self, *, tenant: str, ticket_id: str, to: str, actor: str,
                         actor_caps: set[str], is_operator: bool = False,
                         _max_retries: int = 5) -> Ticket:
        # ATOMIC state move. The old path was fetch -> validate -> save (a plain put): two concurrent
        # callers could both read status `A`, both find A->B legal, and both write B, each appending a
        # status_changed event — the log then shows the ticket moved twice from the same base, and a
        # capability check that was valid for one move was silently reused for another. Now the write is
        # a compare-and-set on the row version; a lost race REFETCHES and re-validates against the NEW
        # state, so the second mover is judged on where the ticket actually is, not where it was.
        for _ in range(max(1, _max_retries)):
            t = await self._fetch(tenant, ticket_id)
            tt = self._wf.require(t.type)
            if t.status == to:
                return t                    # idempotent: already there, no event, no notify
            if not tt.can_transition(t.status, to):
                raise InvalidTransition(f"{t.type}: cannot move {t.status!r} -> {to!r}")
            needed = tt.capability_for(to, self._operator_cap)
            if needed not in actor_caps:
                raise TransitionForbidden(f"transition to {to!r} requires {needed!r}")
            actor_ref = self.pseudonym(tenant, actor)   # operators pseudonymized too (r2-02)
            frm = t.status
            t.status = to
            t.touch()
            if not await self._tickets.cas_save(t):
                continue                    # another writer moved first — refetch and re-validate
            # `resolved` marks a move to a TERMINAL status, so a KPI read (kpis.ticket_kpis) can derive
            # resolution time from the log without knowing this type's workflow — the log carries it.
            data = {"from": frm, "to": to, "resolved": to in tt.terminal}
            await self._events.append(tenant=tenant, ticket_id=t.id, kind="status_changed",
                                      actor_ref=actor_ref, data=data)
            await self._after(t, tt, "status_changed", actor_ref=actor_ref, data=data)
            return t
        raise InvalidTransition(f"{ticket_id}: concurrent transitions could not converge")

    # ── escalate ────────────────────────────────────────────────────────────────
    async def escalate(self, *, tenant: str, ticket_id: str, actor: str, level: str = "",
                        reason: str = "", is_operator: bool = True) -> Ticket:
        """Record that a ticket was ESCALATED — a first-class support fact, not a guess derived from a
        status name. It appends an `escalated` event (with an optional severity level) so 'was it
        escalated, when, how far' is answerable from the log for an SLA/stipend KPI. Idempotent-friendly
        by nature: each call records one escalation; a KPI keys on the FIRST."""
        actor_ref = self.pseudonym(tenant, actor)   # operators pseudonymized too (r2-02)
        # CAS-safe: escalate no longer clobbers a concurrent status transition (r2-03).
        t = await self._mutate(tenant, ticket_id, lambda t: {
            "kind": "escalated", "actor_ref": actor_ref, "body": reason, "data": {"level": level}})
        await self._after(t, self._wf.require(t.type), "escalated", actor_ref=actor_ref)
        return t

    # ── assign / tag / rate ─────────────────────────────────────────────────────
    async def assign(self, *, tenant: str, ticket_id: str, assignee: str, actor: str) -> Ticket:
        actor_ref = self.pseudonym(tenant, actor)

        def _apply(t: Ticket) -> dict:
            t.assignee = assignee
            return {"kind": "assigned", "actor_ref": actor_ref, "data": {"assignee": assignee}}
        return await self._mutate(tenant, ticket_id, _apply)

    async def tag(self, *, tenant: str, ticket_id: str, tags: list[str], actor: str) -> Ticket:
        actor_ref = self.pseudonym(tenant, actor)

        def _apply(t: Ticket) -> dict:
            t.tags = sorted({*t.tags, *[s for s in tags if s]})
            return {"kind": "tagged", "actor_ref": actor_ref, "data": {"tags": t.tags}}
        return await self._mutate(tenant, ticket_id, _apply)

    async def rate(self, *, tenant: str, ticket_id: str, rater: str, score: int,
                   comment: str = "") -> Ticket:
        score = max(1, min(5, int(score)))
        actor_ref = self.pseudonym(tenant, rater)
        return await self._mutate(tenant, ticket_id, lambda t: {
            "kind": "rated", "actor_ref": actor_ref, "body": comment, "data": {"score": score}})

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
