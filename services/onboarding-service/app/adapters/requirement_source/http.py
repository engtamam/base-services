"""HTTP source — ask the owning service, over the internal tier.

Config-mapped like every other outbound call on this platform: the URL comes from
ONBOARDING_SOURCES, never from a request, so no caller can point a requirement check at a host of
their choosing.

The response mapping is deliberately narrow. A source answers with a JSON object and we read one
boolean-ish field from a configured path. Anything we cannot read is UNKNOWN — not satisfied, not
unsatisfied. A source that returns 200 with a body we do not understand has told us nothing, and
guessing in either direction is worse than saying so.
"""
from __future__ import annotations
import asyncio

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from app.ports.requirement_source import SATISFIED, UNKNOWN, UNSATISFIED, Answer

_TRUE = frozenset({"true", "1", "yes", "verified", "active", "complete", "completed"})
_FALSE = frozenset({"false", "0", "no", "none", "pending", "expired", "refused", ""})


class HttpRequirementSource:
    driver = "http"

    def __init__(self, service: str, base_url: str, token: str, path_template: str,
                 answer_path: str = "satisfied", timeout: int = 5) -> None:
        self._service = service
        self._base = base_url.rstrip("/")
        self._token = token
        self._path = path_template
        self._answer_path = answer_path
        self._timeout = timeout

    async def check(self, *, fact: str, tenant: str, subject: str) -> Answer:
        # Percent-encode each value before it goes into the path: subject is caller-influenced
        # (/admin/inspect takes it as a query param), so a raw '../', '?', '#' or '/' would rewrite
        # the request target (path traversal / parameter injection against an internal endpoint).
        q = lambda v: urllib.parse.quote(str(v), safe="")  # noqa: E731
        path = (self._path.replace("{fact}", q(fact)).replace("{tenant}", q(tenant))
                .replace("{subject}", q(subject)))
        url = self._base + path
        req = urllib.request.Request(url, method="GET",
                                     headers={"X-Internal-Token": self._token} if self._token else {})
        try:
            def _send():  # blocking urllib OFF the event loop
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # nosec B310 - config-only host
                    return resp.status, resp.read()
            status, raw = await asyncio.to_thread(_send)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                # The owning service says there is no such record. That is a real answer: the fact
                # is not true. A missing user has not verified their email.
                return Answer(status=UNSATISFIED, detail=f"{self._service}: not found")
            return Answer(status=UNKNOWN, detail=f"{self._service}: HTTP {exc.code}")
        except (urllib.error.URLError, OSError) as exc:
            return Answer(status=UNKNOWN, detail=f"{self._service} unreachable: {exc}")

        if not 200 <= status < 300:
            return Answer(status=UNKNOWN, detail=f"{self._service}: HTTP {status}")
        try:
            payload = json.loads(raw or b"{}")
        except (ValueError, TypeError):
            return Answer(status=UNKNOWN, detail=f"{self._service}: unreadable body")
        if not isinstance(payload, dict):
            return Answer(status=UNKNOWN, detail=f"{self._service}: body was not an object")

        value = _dig(payload, self._answer_path)
        if isinstance(value, bool):
            return Answer(status=SATISFIED if value else UNSATISFIED)
        text = str(value).strip().casefold() if value is not None else ""
        if text in _TRUE:
            return Answer(status=SATISFIED)
        if text in _FALSE:
            return Answer(status=UNSATISFIED)
        # A value nobody taught us to read. Unknown is unknown — mapping it to satisfied is how a
        # renamed field silently completes everyone's onboarding.
        return Answer(status=UNKNOWN,
                      detail=f"{self._service}: cannot read {self._answer_path!r} ({value!r})")


def _dig(payload: dict, path: str) -> Any:
    node: Any = payload
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node
