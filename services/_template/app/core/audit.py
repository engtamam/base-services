"""Auditor — emit a declared action, and decide what happens when the log cannot be reached.

The transport question (38.1) was "how does an event travel". This is the harder one: **what does
a service do when it performs a privileged action and cannot record it?**

Two honest answers exist, and both cost something real:

  * **Fail closed** — refuse the action. The log and reality never diverge, and the platform can
    always answer "who did this?". The cost is availability: an audit outage becomes a partial
    platform outage, and a service that refuses to suspend an abusive user because a logging
    sidecar is down is its own kind of failure.

  * **Fail open** — perform the action and lose the record. The platform stays up. The cost is that
    an action happened which nobody can ever prove, and the gap is invisible: the chain still
    verifies, because a missing event leaves no hole. That is the property that makes this
    dangerous — an audit log cannot tell you what it never received.

The rule here, and the reasoning:

    **Security-relevant actions fail CLOSED. Everything else fails OPEN and is counted.**

The split follows what the record is FOR. Nobody reconstructs an incident from "user updated their
display name"; investigations turn on permission grants, credential changes, data exports, account
suspensions, configuration of security controls. For those, an unprovable action is worse than a
refused one — a permission grant nobody can attribute is indistinguishable from an attacker's, and
that is precisely the moment the log has to work. For routine actions the same trade lands the
other way: taking the platform down to record a display-name change protects nothing.

Every service declares which of its actions are `critical`, so this is a judgement made per action
in review, not a global switch nobody revisits.

Two rules that keep the fail-open path honest:

  * **A dropped event is COUNTED and surfaced through telemetry.** Failing open silently would
    rebuild the exact hole this phase exists to close, one level down.
  * **`null` never counts as recorded.** A sink that records nothing must fail a critical action in
    production exactly as an unreachable audit-service would — which is why the boot guard refuses
    `null` in production rather than relying on anyone reading this file.
"""
from __future__ import annotations

from typing import Any

from app.ports.audit_sink import AuditEvent, AuditReceipt


class AuditUnavailable(RuntimeError):
    """A critical action could not be witnessed, so it must not proceed.

    Callers let this propagate: the API layer turns it into a 503. It deliberately names no action
    detail, because the error text reaches a client and the fact that a specific privileged action
    was ATTEMPTED is itself information.
    """


class Auditor:
    def __init__(self, *, sink, service: str, critical_actions: frozenset[str] = frozenset(),
                 fail_closed: bool = True, telemetry=None) -> None:
        self._sink = sink
        self._service = service
        self._critical = critical_actions
        self._fail_closed = fail_closed
        self._telemetry = telemetry

    async def record(self, action: str, *, actor: str = "", resource: str = "", tenant: str = "",
                     ip: str = "", metadata: dict[str, Any] | None = None) -> AuditReceipt:
        """Witness one action. Raises AuditUnavailable only when a CRITICAL action cannot be
        recorded and the service is configured to fail closed."""
        event = AuditEvent(service=self._service, action=action, actor=actor, resource=resource,
                           tenant=tenant, ip=ip, metadata=metadata or {})
        try:
            receipt = await self._sink.emit(event)
        except Exception as exc:
            # The port forbids raising, but a sink is ordinary code and this is the one call site
            # where a bug must not take down the action it was witnessing.
            receipt = AuditReceipt(status="failed", detail=f"audit sink raised: {exc}")

        if receipt.is_recorded:
            return receipt

        self._count_gap(action, receipt)
        if action in self._critical and self._fail_closed:
            raise AuditUnavailable(
                "this action requires an audit record and the audit log is unavailable")
        return receipt

    def is_critical(self, action: str) -> bool:
        return action in self._critical

    def _count_gap(self, action: str, receipt: AuditReceipt) -> None:
        """An action happened (or was refused) with no record. Failing open SILENTLY would rebuild
        the hole this phase closed, so every gap is counted even when it is tolerated."""
        if self._telemetry is None:
            return
        try:
            self._telemetry.event("audit.gap", {
                "service": self._service, "action": action, "status": receipt.status,
                "critical": action in self._critical, "detail": receipt.detail})
        except Exception:
            pass          # observability must never break the action, or the audit, or both
