"""HTTP sink — POST the event to audit-service's ingest and keep the receipt.

stdlib urllib, matching the other internal clients: no extra runtime dependency and a leaner image.
The URL comes from CONFIG only, never from a request, so no caller can redirect the audit trail at
a host of their choosing — which would be a neat way to make an action look witnessed while the
evidence went somewhere the operator cannot read.

Two things this deliberately does NOT do:

**It does not retry.** The chain assigns `seq` server-side and the ingest contract carries no
idempotency key, so a retry after a timeout cannot be distinguished from a second action — it would
append a DUPLICATE event to a tamper-evident log. A log that invents actions that never happened is
no more trustworthy than one that loses them, and it is harder to spot. Retrying safely needs an
idempotency key on the ingest contract; until then, one attempt and an honest FAILED.

**It does not raise.** A logging problem must never fail the action it was witnessing. The caller
gets FAILED and the failure policy decides whether the action may proceed unwitnessed.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from app.ports.audit_sink import ACCEPTED, FAILED, AuditEvent, AuditReceipt


class HttpAuditSink:
    driver = "http"

    def __init__(self, url: str, token: str, timeout: int = 5) -> None:
        self._url = url
        self._token = token
        self._timeout = timeout

    async def emit(self, event: AuditEvent) -> AuditReceipt:
        body = json.dumps(event.to_payload()).encode()
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["X-Internal-Token"] = self._token
        req = urllib.request.Request(self._url, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # nosec B310 - config-only host
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return AuditReceipt(status=FAILED, detail=f"audit ingest returned HTTP {exc.code}")
        except (urllib.error.URLError, OSError) as exc:
            return AuditReceipt(status=FAILED, detail=f"audit-service unreachable: {exc}")

        if not 200 <= status < 300:
            return AuditReceipt(status=FAILED, detail=f"audit ingest returned HTTP {status}")
        try:
            receipt = json.loads(raw or b"{}")
        except (ValueError, TypeError):
            # 200 with a body we cannot read is not proof of anything. The seq and hash ARE the
            # evidence of where this sits in the chain; without them we cannot claim it was recorded.
            return AuditReceipt(status=FAILED, detail=f"HTTP {status} with an unreadable receipt")
        if not isinstance(receipt, dict) or not receipt.get("hash"):
            return AuditReceipt(status=FAILED,
                                detail=f"HTTP {status} but no chain hash — nothing proves it landed")
        return AuditReceipt(status=ACCEPTED, id=str(receipt.get("id", "")),
                            seq=int(receipt.get("seq") or 0), hash=str(receipt["hash"]))
