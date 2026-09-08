"""The public WhatsApp Flow data-endpoint route: signature, disabled-by-default, encrypted ping."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest
from starlette.testclient import TestClient

from app.config import get_settings
from app.core import flow_crypto, flow_dispatch as fd

SECRET = "app-secret-xyz"



def _keypair(passphrase: bytes | None = None):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    enc = (serialization.BestAvailableEncryption(passphrase) if passphrase
           else serialization.NoEncryption())
    priv = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, enc)
    pub = key.public_key().public_bytes(serialization.Encoding.PEM,
                                        serialization.PublicFormat.SubjectPublicKeyInfo)
    return priv, pub

def _sign(raw: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()


def _meta_body(payload: dict, public_pem: bytes):
    import os

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    aes, iv = os.urandom(16), os.urandom(12)
    pub = serialization.load_pem_public_key(public_pem)
    ek = pub.encrypt(aes, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                                       algorithm=hashes.SHA256(), label=None))
    ct = AESGCM(aes).encrypt(iv, json.dumps(payload).encode(), None)
    body = {"encrypted_flow_data": base64.b64encode(ct).decode(),
            "encrypted_aes_key": base64.b64encode(ek).decode(),
            "initial_vector": base64.b64encode(iv).decode()}
    return body, aes, iv


def _decrypt(reply_b64: str, aes: bytes, iv: bytes) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    ct = base64.b64decode(reply_b64)
    return json.loads(AESGCM(aes).decrypt(bytes(b ^ 0xFF for b in iv), ct, None).decode())




def _seed_key(client, private_pem: bytes) -> None:
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(client.app.state.secret_store.set(
            tenant="", name="WHATSAPP_FLOW_PRIVATE_KEY", value=private_pem.decode()))
    finally:
        loop.close()


def _app(monkeypatch, *, enabled=True):
    monkeypatch.setenv("META_APP_SECRET", SECRET)
    monkeypatch.setenv("SECRETS_KEK", "0" * 64)
    monkeypatch.setenv("PII_SALT", "pepper")
    if enabled:
        monkeypatch.setenv("WHATSAPP_FLOW_ENDPOINT_ENABLED", "true")
    get_settings.cache_clear()
    from app.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


def test_disabled_by_default_is_503(monkeypatch):
    c = _app(monkeypatch, enabled=False)
    r = c.post("/webhooks/whatsapp/flow", content=b"{}", headers={"X-Hub-Signature-256": _sign(b"{}")})
    assert r.status_code == 503
    get_settings.cache_clear()


def test_bad_signature_is_401(monkeypatch):
    c = _app(monkeypatch)
    r = c.post("/webhooks/whatsapp/flow", content=b"{}",
               headers={"X-Hub-Signature-256": "sha256=deadbeef"})
    assert r.status_code == 401
    get_settings.cache_clear()


def test_enabled_but_no_key_is_503(monkeypatch):
    c = _app(monkeypatch)
    raw = json.dumps({"encrypted_flow_data": "x", "encrypted_aes_key": "y",
                      "initial_vector": "z"}).encode()
    r = c.post("/webhooks/whatsapp/flow", content=raw, headers={"X-Hub-Signature-256": _sign(raw)})
    assert r.status_code == 503
    get_settings.cache_clear()


def test_encrypted_ping_round_trips(monkeypatch):
    c = _app(monkeypatch)
    priv, pub = _keypair()
    # seed the private key into the secret store the route reads
    _seed_key(c, priv)

    body, aes, iv = _meta_body({"version": "3.0", "action": "ping"}, pub)
    raw = json.dumps(body).encode()
    r = c.post("/webhooks/whatsapp/flow", content=raw, headers={"X-Hub-Signature-256": _sign(raw)})
    assert r.status_code == 200, r.text
    assert _decrypt(r.text, aes, iv) == {"data": {"status": "active"}}
    get_settings.cache_clear()


def test_a_wrong_key_returns_421_for_meta_to_refresh(monkeypatch):
    c = _app(monkeypatch)
    priv, pub = _keypair()
    other_priv, _ = _keypair()
    _seed_key(c, other_priv)
    body, aes, iv = _meta_body({"action": "ping"}, pub)
    raw = json.dumps(body).encode()
    r = c.post("/webhooks/whatsapp/flow", content=raw, headers={"X-Hub-Signature-256": _sign(raw)})
    assert r.status_code == 421
    get_settings.cache_clear()


def test_full_flow_endpoint_drives_the_booking_engine(monkeypatch):
    """End-to-end: an encrypted INIT through the public route returns the first booking screen."""
    import json as _json

    from app.adapters.datastore.memory import MemoryDataStore
    from app.core.booking_flow import BookingEngine, FlowDefinition, SessionStore

    class _Avail:
        async def options(self, *, tenant, dimension, chosen):
            return [{"id": "cut", "title": "Haircut"}]
        async def calendar(self, *, tenant, chosen):
            return {"min_date": "2026-09-08", "max_date": "2026-10-08", "unavailable_dates": []}
        async def slots(self, *, tenant, chosen, date):
            return [{"id": "s1", "title": "10:00"}]

    class _Booking:
        async def reserve(self, *, tenant, selection, idempotency_key):
            return {"ok": True, "ref": "BK9"}

    c = _app(monkeypatch)
    priv, pub = _keypair()
    _seed_key(c, priv)
    defn = FlowDefinition(steps=({"dimension": "service"}, {"dimension": "date"},
                                 {"dimension": "time"}, {"dimension": "confirm"}))

    class _Defs:
        async def get(self, tenant):
            return defn
    engine = BookingEngine(definitions=_Defs(), availability=_Avail(), booking=_Booking(),
                           sessions=SessionStore(MemoryDataStore(), clock=lambda: 1000.0))
    from app.core.flow_registry import FlowRegistry
    reg = FlowRegistry()
    reg.register(engine)
    c.app.state.flow_registry = reg

    body, aes, iv = _meta_body({"version": "3.0", "action": "INIT", "flow_token": "tk"}, pub)
    raw = _json.dumps(body).encode()
    r = c.post("/webhooks/whatsapp/flow", content=raw, headers={"X-Hub-Signature-256": _sign(raw)})
    assert r.status_code == 200, r.text
    reply = _decrypt(r.text, aes, iv)
    assert reply["screen"] == "SELECT" and reply["data"]["options"][0]["id"] == "cut"
    get_settings.cache_clear()
