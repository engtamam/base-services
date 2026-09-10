"""HTTP provisioner — create and un-create through another service's internal tier.

Config-mapped like every other outbound call here: a platform points each provisioning kind at its
own service, so this works for tenant-service and identity-service as shipped, and equally for a
customer's own provisioning API.

The undo path is the part worth reading. It treats **404 as success**, because a compensation that
failed on "already gone" would loop forever on the one step that needs no work — and a resumed
rollback re-attempts undos that already succeeded, so already-gone is the NORMAL case on retry, not
an exception.
"""
from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request

from app.ports.provisioner import CREATED, EXISTS, FAILED, ProvisionRequest, ProvisionResult


class HttpProvisioner:
    driver = "http"

    def __init__(self, kind: str, create_url: str, undo_url: str, token: str,
                 timeout: int = 10) -> None:
        self._kind = kind
        self._create_url = create_url
        self._undo_url = undo_url
        self._token = token
        self._timeout = timeout

    async def create(self, request: ProvisionRequest) -> ProvisionResult:
        body = {"tenant": request.tenant, "subject": request.subject,
                "idempotency_key": request.key, **request.attributes}
        # _call is a BLOCKING urlopen — off the loop, or a slow downstream freezes the whole worker.
        status, payload, error = await asyncio.to_thread(self._call, self._create_url, body)
        if error:
            return ProvisionResult(status=FAILED, detail=error)
        if status == 409:
            # Already ours. The downstream service deduped on our key, which is the idempotency
            # contract working — not a conflict to compensate for.
            return ProvisionResult(status=EXISTS,
                                   reference=str((payload or {}).get("id", "")),
                                   detail="already provisioned for this key")
        if not 200 <= status < 300:
            return ProvisionResult(status=FAILED, detail=f"{self._kind}: HTTP {status}")
        reference = str((payload or {}).get("id", ""))
        if not reference:
            # Created something we cannot later undo. Reported as failure so the Saga compensates
            # NOW, rather than leaving an unreferenced resource nobody can find or delete.
            return ProvisionResult(status=FAILED,
                                   detail=f"{self._kind}: created with no id in the response")
        return ProvisionResult(status=CREATED, reference=reference)

    async def undo(self, request: ProvisionRequest, reference: str) -> ProvisionResult:
        if not reference:
            return ProvisionResult(status=CREATED, detail="nothing was created")
        url = self._undo_url.replace("{id}", reference)
        status, _, error = await asyncio.to_thread(
            self._call, url, {"idempotency_key": request.key}, method="DELETE")
        if error:
            return ProvisionResult(status=FAILED, detail=error)
        if status == 404 or 200 <= status < 300:
            # 404 means it is already gone, which is what we wanted.
            return ProvisionResult(status=CREATED, reference=reference)
        return ProvisionResult(status=FAILED, detail=f"undo {self._kind}: HTTP {status}")

    def _call(self, url: str, body: dict, method: str = "POST"):
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), method=method,
            headers={"Content-Type": "application/json", "X-Internal-Token": self._token})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # nosec B310 - config-only host
                raw = resp.read()
                try:
                    return resp.status, json.loads(raw or b"{}"), None
                except (ValueError, TypeError):
                    return resp.status, {}, None
        except urllib.error.HTTPError as exc:
            body_text = {}
            try:
                body_text = json.loads(exc.read() or b"{}")
            except Exception:
                pass
            return exc.code, body_text, None
        except (urllib.error.URLError, OSError) as exc:
            return 0, {}, f"{self._kind} unreachable: {exc}"
