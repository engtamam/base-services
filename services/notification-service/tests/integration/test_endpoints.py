"""The three endpoint tiers respond correctly."""
from __future__ import annotations

from tests.conftest import INTERNAL_TOKEN

_H = {"X-Internal-Token": INTERNAL_TOKEN}


class _Pool:
    """A provider pool scripted for the register precondition checks."""
    def __init__(self, token="", phone="", pin=""):
        self._token, self._phone, self._pin = token, phone, pin

    async def whatsapp_access_token(self, tenant):
        return self._token

    async def whatsapp_phone_number_id(self, tenant):
        return self._phone

    async def whatsapp_pin(self, tenant, phone_id):
        return self._pin


def test_whatsapp_register_not_connected_is_409_matching_status(client):
    # Item 7: the SAME NOT_CONNECTED refusal that /whatsapp/status returns 409 for must be 409 here
    # too — it used to be 400, forcing every consumer to special-case one of the two siblings and read
    # a refusal from the wrong one as a success. A missing stored token is a state conflict, not a
    # malformed request.
    client.app.state.provider_pool = _Pool(token="", phone="")
    r = client.post("/internal/whatsapp/register", json={"tenant": "t1"}, headers=_H)
    assert r.status_code == 409 and r.json()["reason"] == "NOT_CONNECTED"


def test_whatsapp_register_no_pin_is_409(client):
    # Same reasoning: connected but no PIN available is a precondition/state conflict, not a 400.
    client.app.state.provider_pool = _Pool(token="tok", phone="pid", pin="")
    r = client.post("/internal/whatsapp/register", json={"tenant": "t1"}, headers=_H)
    assert r.status_code == 409 and r.json()["reason"] == "NO_PIN"


def test_privacy_erase_refuses_an_unindexed_subject_kind(client, monkeypatch):
    # Item 6: a target told the subject is a phone_hash, while it indexes by internal_id, must REFUSE
    # explicitly (unrecognized_subject_kind) rather than match zero rows and ack a false "erased".
    from app import config
    monkeypatch.setenv("PRIVACY_INDEXED_SUBJECT_KINDS", "internal_id")
    monkeypatch.setenv("PII_SALT", "test-salt-0123456789")   # the accepted branch runs the real eraser
    config.get_settings.cache_clear()
    try:
        r = client.post("/internal/privacy/erase",
                        json={"tenant": "t1", "subject": "u_7", "subject_kind": "phone_hash"},
                        headers=_H)
        assert r.status_code == 200
        body = r.json()
        assert body["unrecognized_subject_kind"] is True and body["erased"] == []
        assert body["subject_kind"] == "phone_hash"
        # A kind it DOES index is accepted and erases as usual (no refusal flag).
        ok = client.post("/internal/privacy/erase",
                         json={"tenant": "t1", "subject": "u_7", "subject_kind": "internal_id"},
                         headers=_H)
        assert ok.status_code == 200 and "unrecognized_subject_kind" not in ok.json()
    finally:
        config.get_settings.cache_clear()


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_readyz_ready_with_no_deps(client):
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"


def test_metrics_prometheus(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]


def test_internal_manifest_requires_token(client):
    assert client.get("/internal/manifest").status_code == 401
    assert client.get("/internal/health").status_code == 401


def test_internal_manifest_with_token(client):
    r = client.get("/internal/manifest", headers={"X-Internal-Token": INTERNAL_TOKEN})
    assert r.status_code == 200
    body = r.json()
    assert body["name"]
    assert "endpoints" in body and "ports" in body and "events" in body


def test_internal_rejects_wrong_token(client):
    r = client.get("/internal/health", headers={"X-Internal-Token": "wrong"})
    assert r.status_code == 401
