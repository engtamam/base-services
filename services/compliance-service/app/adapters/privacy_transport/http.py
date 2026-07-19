"""HTTP transport — POST the erase request to a target and classify its answer.

stdlib urllib, matching the job-http orchestrator: no extra runtime dependency, and the image stays
lean. The URL always comes from CONFIG (PRIVACY_TARGETS), never from a request — that is what keeps
this from being an SSRF console: a caller cannot make us POST at a host of their choosing, and the
config parser already refuses anything that is not http(s).

Classification is the whole job, and it is deliberately strict:
    200 + a JSON object -> ACKED   (the body is the evidence of what was erased and what was kept)
    200 + unreadable    -> FAILED  (a target that cannot say what it did has not answered)
    202                 -> PENDING (async target; it will push its ack to /target-ack)
    anything else       -> FAILED  (retried; never mistaken for done)
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from app.ports.privacy_transport import ACKED, FAILED, PENDING, EraseOutcome


class HttpPrivacyTransport:
    driver = "http"

    async def erase(self, *, url: str, token: str, request_id: str, action: str,
                    tenant: str, subject: str, timeout: int) -> EraseOutcome:
        payload = {"tenant": tenant, "subject": subject, "action": action,
                   "request_id": request_id}
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Internal-Token"] = token
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - config-only host
                status = resp.status
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            return EraseOutcome(status=FAILED, detail=f"HTTP {exc.code}")
        except (urllib.error.URLError, OSError) as exc:
            return EraseOutcome(status=FAILED, detail=f"unreachable: {exc}")

        if status == 202:
            return EraseOutcome(status=PENDING, detail="target accepted; awaiting its pushed ack")
        if 200 <= status < 300:
            try:
                ack = json.loads(raw or b"{}")
            except (ValueError, TypeError):
                return EraseOutcome(status=FAILED, detail=f"HTTP {status} with unreadable ack body")
            if not isinstance(ack, dict):
                return EraseOutcome(status=FAILED, detail=f"HTTP {status} ack was not an object")
            return EraseOutcome(status=ACKED, ack=ack)
        return EraseOutcome(status=FAILED, detail=f"HTTP {status}")
