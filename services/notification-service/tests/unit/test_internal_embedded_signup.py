"""The INTERNAL Embedded Signup route — the twin that lets a product service run the exchange FOR a
shop owner (who can't hold platform.secrets). Same core exchange; tenant rides in the body; guarded
by X-Internal-Token; the token still never appears in the response."""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

import app.core.whatsapp_onboarding as wo
from app.config import get_settings
from app.main import create_app


class _FakeOnboarding:
    """Stands in for WhatsAppOnboarding so the route can be exercised without Meta or a KEK."""
    last = {}

    def __init__(self, *a, **k):
        pass

    async def exchange(self, *, tenant, code, waba_id, phone_number_id, actor_ref=""):
        _FakeOnboarding.last = {"tenant": tenant, "waba_id": waba_id, "code": code}
        return {"ok": True, "subscribed": True}


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("SECRETS_KEK", "k" * 32)          # so _secret_store builds
    get_settings.cache_clear()
    monkeypatch.setattr(wo, "WhatsAppOnboarding", _FakeOnboarding)
    # the operator helper imports the class lazily from wo, so patching wo is enough
    app = create_app()
    yield TestClient(app)
    get_settings.cache_clear()


def _tok():
    return {"X-Internal-Token": get_settings().INTERNAL_SERVICE_TOKEN}


def test_requires_the_internal_token():
    c = TestClient(create_app())
    r = c.post("/internal/whatsapp/embedded-signup",
               json={"code": "c", "waba_id": "w", "phone_number_id": "p", "tenant": "acme"})
    assert r.status_code == 401


def test_runs_the_exchange_with_the_body_tenant_and_hides_the_token(client):
    r = client.post("/internal/whatsapp/embedded-signup", headers=_tok(),
                    json={"code": "CODE30S", "waba_id": "W1", "phone_number_id": "P1", "tenant": "acme"})
    assert r.status_code == 200 and r.json() == {"ok": True, "subscribed": True}
    assert _FakeOnboarding.last["tenant"] == "acme"       # tenant came from the BODY, not a token
    assert "CODE30S" not in r.text and "access_token" not in r.text
