"""Recording provider — no network. Serves scripted pages, records pushes (pilot/tests)."""
from __future__ import annotations

from typing import Any

from app.ports.connector_provider import Page, Tokens


class RecordingConnector:
    driver = "recording"

    def __init__(self) -> None:
        self.pushed: list[dict] = []
        self.pages: dict[str, list[Page]] = {}
        self.refresh_calls = 0

    def exchange(self, *, code, redirect_uri, spec) -> Tokens:
        return Tokens(access_token="rec_access", refresh_token="rec_refresh", expires_in=3600)

    def refresh(self, *, refresh_token, spec) -> Tokens:
        self.refresh_calls += 1
        # simulate a ROTATING provider: a brand-new refresh token each time
        return Tokens(access_token=f"rec_access_{self.refresh_calls}",
                      refresh_token=f"rec_refresh_{self.refresh_calls}", expires_in=3600)

    def fetch(self, *, access_token, resource, cursor, limit, spec) -> Page:
        queue = self.pages.get(resource, [])
        return queue.pop(0) if queue else Page(records=[], next_cursor="")

    def push(self, *, access_token, resource, record, idempotency_key, spec) -> dict[str, Any]:
        self.pushed.append({"resource": resource, "record": record, "key": idempotency_key})
        return {"ok": True}

    def revoke(self, *, access_token, spec) -> bool:
        return True
