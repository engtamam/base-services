"""/internal/privacy/erase, driven over HTTP against the real (memory) datastore.

The cheapest possible DSAR assertion, and the one that was missing: post an erase for a subject that
has NO data and assert a clean 200 with already_absent. The endpoint walked an UNSCOPED collection
with find({}), which the datastore refuses (find requires a non-empty where) — so every call, even
this no-data one, returned 500, and a DSAR fan-out that included this service could never close
complete. The unit test used a hand-rolled fake that accepted find({}), so it passed while the real
path 500'd; this drives the actual app against the memory adapter, which enforces the same contract
as postgres, so it exercises the real code path with no fixtures.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

ITOKEN = "t" * 40


def _make_app() -> TestClient:
    from app.config import get_settings
    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN=ITOKEN, RATELIMIT_MAX="1000",
                      PII_SALT="s" * 32)
    get_settings.cache_clear()
    from app.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture
def client():
    from app.config import get_settings
    saved = dict(os.environ)
    c = _make_app()
    yield c
    os.environ.clear(); os.environ.update(saved); get_settings.cache_clear()


def _h():
    return {"X-Internal-Token": ITOKEN}


def test_erase_requires_the_internal_token(client):
    assert client.post("/internal/privacy/erase",
                       json={"tenant": "t", "subject": "s"}).status_code == 401


def test_erasing_a_subject_with_no_data_acks_already_absent(client):
    # The no-data path: nothing to redact anywhere. It must be a clean 200 already_absent — NOT the
    # 500 the unscoped find({}) produced on every call.
    r = client.post("/internal/privacy/erase", headers=_h(),
                    json={"tenant": "tnt_alpha", "subject": "nobody@example.com"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["already_absent"] is True
    assert body["erased"] == []
    assert body["service"] == "webhook-service"


def test_the_unscoped_inbound_collection_is_actually_walked(client):
    # Seed a matching subject into the UNSCOPED inbound collection (no tenant), then erase and prove
    # the payload was redacted — the path that used to raise before it could touch a single row.
    import asyncio
    st = client.app.state.container.datastore
    subject = "alice@example.com"
    asyncio.new_event_loop().run_until_complete(
        st.put("inbound", "in1", {"id": "in1", "source": "stripe", "provider_event_id": "evt_1",
                                  "payload": {"email": subject}}))
    r = client.post("/internal/privacy/erase", headers=_h(),
                    json={"tenant": "tnt_alpha", "subject": subject})
    assert r.status_code == 200, r.text
    assert r.json()["already_absent"] is False   # the inbound row matched and was redacted
    row = asyncio.new_event_loop().run_until_complete(st.get("inbound", "in1"))
    assert subject not in str(row["payload"])
