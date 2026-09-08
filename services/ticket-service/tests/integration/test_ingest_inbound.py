"""POST /internal/tickets/ingest-inbound — the scheduler-driven bridge: token-gated, off by default,
and when enabled it turns a batch of inbound messages into tickets (open + append) via the container's
inbound source."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.core.inbound_bridge import InboundMessage

TOK = {"X-Internal-Token": "t" * 40}


class _FakeSource:
    driver = "fake"

    def __init__(self, *batches):
        self._batches = list(batches)
        self.i = 0

    async def read(self, *, after, limit):
        if self.i >= len(self._batches):
            return [], None
        b = self._batches[self.i]
        self.i += 1
        return b


def _app(enabled: bool):
    saved = dict(os.environ)
    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN="t" * 40, PII_SALT="s" * 32,
                      TICKET_INBOUND_ENABLED="true" if enabled else "false")
    get_settings.cache_clear()
    from app.main import create_app
    return TestClient(create_app()), saved


def _restore(saved):
    os.environ.clear(); os.environ.update(saved); get_settings.cache_clear()


def test_requires_the_internal_token():
    c, saved = _app(enabled=True)
    try:
        assert c.post("/internal/tickets/ingest-inbound").status_code == 401
    finally:
        _restore(saved)


def test_disabled_by_default_is_503():
    c, saved = _app(enabled=False)
    try:
        assert c.post("/internal/tickets/ingest-inbound", headers=TOK).status_code == 503
    finally:
        _restore(saved)


def _msg(eid, sender="966500", text="hi", tenant="acme"):
    return InboundMessage(channel="whatsapp", tenant=tenant, sender=sender, external_id=eid,
                          text=text, timestamp=1.0)


def test_ingests_a_batch_into_tickets_and_advances_the_cursor():
    c, saved = _app(enabled=True)
    try:
        # two messages from the same sender: first opens, second appends; cursor advances to "cur1"
        c.app.state.container.inbound_source = _FakeSource(
            ([_msg("w1", text="hello"), _msg("w2", text="anyone?")], "cur1"))
        r = c.post("/internal/tickets/ingest-inbound", headers=TOK)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["read"] == 2 and body["created"] == 1 and body["appended"] == 1
        assert body["cursor"] == "cur1"
        # a second run with nothing new is a clean no-op
        c.app.state.container.inbound_source = _FakeSource()
        assert c.post("/internal/tickets/ingest-inbound", headers=TOK).json()["read"] == 0
    finally:
        _restore(saved)
