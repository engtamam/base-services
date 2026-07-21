"""In-process sink — audit-service records ITS OWN actions by calling its own core, not over HTTP.

Every other service reaches the chain over HTTP: it POSTs to audit-service's ingest. audit-service
is the one that must NOT, and doing so was a real production hang, not a theoretical one.

    the http sink POSTs to AUDIT_SERVICE_URL — which, for audit-service, is itself
    → a request handler occupies a worker while it makes an HTTP call back to the same service
    → under load every worker can be busy, so the self-call finds no free event loop
    → it times out, the action was `subject.erased` (critical, fail-closed), and the erase 503s

That is exactly what an adopter hit: external ingest returned 200 (a free worker answered), so it
looked like a contract bug, while audit's own erase 503'd because it could not witness itself.
Adding workers only widens the window — it does not close it.

The fix is to stop leaving the building. The core AuditService is in this process; recording is a
local, atomic append to the hash chain. So this sink calls it directly. No socket, no worker, no
timeout, no deadlock — and it is strictly MORE correct, because a tamper-evident write should never
depend on a network round-trip that can fail half-way and cannot be safely retried.

It is the DEFAULT for audit-service and should stay so. `http`/`eventbus` remain for the exotic
case of shipping a service's audit to a SEPARATE audit cluster; pointing audit-service's http sink
back at itself is the configuration this exists to make unnecessary.
"""
from __future__ import annotations

from typing import Any

from app.core.audit_repository import AuditRepository
from app.core.audit_service import AuditService
from app.core.pii_policy import PiiPolicy, parse_fields
from app.ports.audit_sink import ACCEPTED, FAILED, AuditEvent, AuditReceipt


class InProcessAuditSink:
    driver = "in_process"

    def __init__(self, *, datastore, integrity, settings) -> None:
        # One AuditService, reused: it is stateless over the datastore and integrity it wraps, and
        # `record` serialises through the store's own head/append, so a shared instance is safe.
        # The PII policy is applied here for the SAME reason it is on the request path — the
        # pseudonym rule must hold at the one boundary no caller can bypass, self-audit included.
        self._svc = AuditService(
            repo=AuditRepository(datastore), integrity=integrity, exporters={},
            default_format=settings.EXPORTER_DEFAULT, partition_mode=settings.AUDIT_PARTITION,
            query_limit=settings.AUDIT_QUERY_LIMIT, export_limit=settings.AUDIT_EXPORT_LIMIT,
            pii_policy=PiiPolicy(mode=settings.AUDIT_PII_MODE,
                                 fields=parse_fields(settings.AUDIT_PII_FIELDS),
                                 salt=settings.PII_SALT),
        )

    async def emit(self, event: AuditEvent) -> AuditReceipt:
        """Append the event to the local chain and return the seq+hash that prove where it sits.

        Never raises — a logging failure must not fail the action it witnesses (same contract as
        every sink). A failure here would be a storage fault, not a network one, and it comes back
        as FAILED for the fail-closed policy to weigh, exactly as an HTTP failure would.
        """
        try:
            evt = await self._svc.record(
                service=event.service, action=event.action, actor=event.actor,
                resource=event.resource, tenant=event.tenant, ip=event.ip,
                metadata=event.metadata)
        except Exception as exc:   # noqa: BLE001 — a sink must not raise into the action
            return AuditReceipt(status=FAILED, detail=f"in-process audit append failed: {exc}")
        return AuditReceipt(status=ACCEPTED, id=evt.id, seq=evt.seq, hash=evt.hash)
