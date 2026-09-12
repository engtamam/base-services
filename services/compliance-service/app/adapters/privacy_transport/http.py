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
import asyncio

import json
from app.core.internal_client import assertion_headers_for_url
import urllib.error
import urllib.request

from app.ports.privacy_transport import ACKED, FAILED, PARTIAL, PENDING, EraseOutcome


class HttpPrivacyTransport:
    driver = "http"

    async def erase(self, *, url: str, token: str, request_id: str, action: str,
                    tenant: str, subject: str, timeout: int, subject_kind: str = "") -> EraseOutcome:
        # The subject is passed VERBATIM and must be a real reference. An empty/blank one would erase
        # against nothing and come back already_absent — a clean ack over no work. Refuse it here as a
        # DISTINGUISHABLE failure rather than let a malformed id read as "she was erased".
        if not (subject or "").strip():
            return EraseOutcome(status=FAILED, detail="INVALID_SUBJECT: empty subject reference")
        payload = {"tenant": tenant, "subject": subject, "action": action,
                   "request_id": request_id}
        # Carry the identifier KIND when we know it, so a target that indexes by a different kind can
        # say so instead of silently matching nothing. Only sent when set — a target that ignores it
        # behaves exactly as before (the field is additive and additionalProperties-safe in the schema).
        if subject_kind:
            payload["subject_kind"] = subject_kind
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Internal-Token"] = token
        headers.update(assertion_headers_for_url(url))   # aud = the target's hostname (its own name)
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                     headers=headers)
        try:
            def _send():  # blocking urllib OFF the event loop
                with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - config-only host
                    return resp.status, resp.read()
            status, raw = await asyncio.to_thread(_send)
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
            # UNRECOGNISED SUBJECT KIND == the target does not index by the kind of identifier we sent
            # (it holds internal ids, we sent a phone hash). It matched NOTHING it could recognise, so
            # a 2xx here is NOT an erasure — record it FAILED (never ACKED) so the DSAR stays open and
            # VISIBLE rather than closing over a subject the target never actually found. This is the
            # explicit refusal the contract asks for: a target refuses by setting the flag, and the
            # DSAR-protecting classification is HERE, so it is safe even against a target that returns
            # the flag on a 200. Checked before `truncated`/`already_absent`: a kind it cannot match is
            # not a partial erasure and not a real absence.
            if ack.get("unrecognized_subject_kind"):
                return EraseOutcome(status=FAILED, ack=ack,
                                    detail=(f"target does not index subject_kind={subject_kind!r} "
                                            f"(unrecognized_subject_kind) — refused, not erased"))
            # BLOCKED BY LEGAL HOLD == the target lawfully RETAINED the subject's data under its own
            # hold and erased nothing. It is a 200 with a truthful ack, but it is NOT an erasure — so
            # it must NEVER be classified ACKED, or the target flips to "done" and the DSAR is derived
            # COMPLETED and reported to the regulator as fully erased while the data is deliberately
            # kept. Recorded FAILED (never ACKED), exactly like unrecognized_subject_kind: the target
            # stays unacked, the DSAR stays OPEN and VISIBLE over data that was retained, not erased.
            # Checked before already_absent: retention-under-hold is neither a real absence nor a done.
            if ack.get("blocked_by_legal_hold"):
                return EraseOutcome(status=FAILED, ack=ack,
                                    detail=("target retained the subject's data under its own active "
                                            "legal hold (blocked_by_legal_hold) — not erased; DSAR "
                                            "stays open over retained data"))
            # TRUNCATED == the target erased only part of the subject's rows this call (a bounded
            # batch, a scan cap). It is NOT done — record it PARTIAL so the target stays unacked and
            # the erase re-dispatches idempotently until a call returns without truncated. A scanner
            # that erased 5,000 of 40,000 and said `truncated` was previously recorded as a full ack.
            if ack.get("truncated"):
                return EraseOutcome(status=PARTIAL, ack=ack,
                                    detail="target erased a partial batch (truncated); re-dispatching")
            # A zero-row erasure is legitimate BUT distinguishable: `already_absent` means the target
            # held nothing — which, if it is every target, is the fingerprint of a wrong subject id
            # (the double-hash bug) rather than a real absence. Kept in `detail` so the record shows it.
            detail = "already_absent (held nothing)" if ack.get("already_absent") else ""
            return EraseOutcome(status=ACKED, ack=ack, detail=detail)
        return EraseOutcome(status=FAILED, detail=f"HTTP {status}")
