"""assistant-service over HTTP — advisory triage and reply drafts.

stdlib urllib, config-only host checked against the egress allowlist. Every failure is SWALLOWED to
None: the AI is advisory, so a model that is slow, down, or misbehaving must leave the ticket exactly
as it would be with no AI — never a 500 on a customer opening a ticket. Content is tenant-scoped and
carries the pseudonym, never a raw identity.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from app.ports.suggestion_provider import Draft, Triage

_log = logging.getLogger(__name__)


class HttpSuggestionProvider:
    driver = "http"

    def __init__(self, url: str, token: str, *, allowed_hosts: frozenset[str] = frozenset(),
                 timeout: int = 8, triage_path: str = "/internal/assistant/triage",
                 draft_path: str = "/internal/assistant/draft") -> None:
        if not url:
            raise ValueError("ASSISTANT_SERVICE_URL is required for the http suggest driver")
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        if allowed_hosts and host not in allowed_hosts:
            raise ValueError(f"ASSISTANT_SERVICE_URL host {host!r} is not in EGRESS_ALLOWED_HOSTS")
        self._base = url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._triage_path = triage_path
        self._draft_path = draft_path

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["X-Internal-Token"] = self._token
        req = urllib.request.Request(self._base + path, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # nosec B310 - config-only host
                if not 200 <= resp.status < 300:
                    return None
                data = json.loads(resp.read() or b"{}")
        except (urllib.error.URLError, OSError, ValueError, TypeError) as exc:
            # Advisory: a failure is a shrug, not an error. The ticket proceeds without a suggestion.
            _log.info("assistant_unavailable", extra={"error": type(exc).__name__})
            return None
        return data if isinstance(data, dict) else None

    async def triage(self, *, tenant: str, ticket_type: str, subject: str, body: str,
                     types: tuple[str, ...]) -> Triage | None:
        data = self._post(self._triage_path, {
            "tenant": tenant, "current_type": ticket_type, "subject": subject, "body": body,
            "candidate_types": list(types)})
        if not data:
            return None
        return Triage(
            type=str(data.get("type", "")), priority=str(data.get("priority", "")),
            tags=tuple(str(t) for t in (data.get("tags") or [])),
            confidence=float(data.get("confidence", 0.0) or 0.0),
            summary=str(data.get("summary", "")))

    async def draft(self, *, tenant: str, subject: str, thread: list[dict[str, Any]]) -> Draft | None:
        data = self._post(self._draft_path, {"tenant": tenant, "subject": subject, "thread": thread})
        if not data:
            return None
        return Draft(text=str(data.get("text", "")), confidence=float(data.get("confidence", 0.0) or 0.0))

    async def health(self) -> bool:
        return True
