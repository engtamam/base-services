"""job-http orchestrator — enqueue one durable job per target in job-service.

Durable beats fire-and-forget for erasure: job-service retries, dead-letters, and never
loses the task. The job's `unique_key` makes a re-dispatch idempotent.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

_TIMEOUT = 10


class JobHttpOrchestrator:
    driver = "job-http"

    def __init__(self, base_url: str, token: str) -> None:
        self._base = (base_url or "").rstrip("/")
        self._token = token

    async def dispatch(self, *, request_id: str, action: str, tenant: str, subject: str,
                       target: dict[str, Any]) -> bool:
        if not (self._base and self._token):
            return False                                   # unconfigured → not dispatched
        service = target.get("service", "")
        payload = {
            "queue": "compliance", "tenant": tenant, "type": f"compliance.{action}",
            "payload": {"request_id": request_id, "subject": subject, "service": service,
                        "category": target.get("category")},
            "unique_key": f"{action}:{request_id}:{service}",   # idempotent re-dispatch
        }
        req = urllib.request.Request(
            f"{self._base}/internal/jobs/enqueue", data=json.dumps(payload).encode(),
            method="POST", headers={"Content-Type": "application/json",
                                    "X-Internal-Token": self._token})
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # nosec B310 - fixed config host
                return 200 <= resp.status < 300
        except (urllib.error.HTTPError, OSError, Exception):
            return False
