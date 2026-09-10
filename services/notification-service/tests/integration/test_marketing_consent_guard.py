"""A marketing template MUST be mapped to a consent purpose — enforced at write AND boot (option أ).

Closes the gap where a marketing template a developer forgot to add to NOTIFY_CONSENT_PURPOSES
shipped as unchecked "transactional". category is the source of truth; the guards make the
misconfiguration impossible to introduce (write) or deploy (boot).
"""
from __future__ import annotations

import datetime as dt
import json
import os

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "h" * 40
ITOKEN = "i" * 40


def _make_app(**over) -> TestClient:
    from app.config import get_settings
    env = {"ENV": "test", "INTERNAL_SERVICE_TOKEN": ITOKEN, "TOKEN_VERIFIER_DRIVER": "hs256",
           "TOKEN_HS256_SECRET": SECRET, "RATELIMIT_MAX": "1000"}
    env.update(over)
    os.environ.update(env)
    get_settings.cache_clear()
    from app.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


def _admin():
    now = dt.datetime.now(dt.timezone.utc)
    tok = jwt.encode({"sub": "admin", "caps": ["platform.notifications"],
                      "exp": now + dt.timedelta(minutes=5), "iat": now}, SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
def env_guard():
    saved = dict(os.environ)
    yield
    os.environ.clear(); os.environ.update(saved)
    from app.config import get_settings
    get_settings.cache_clear()


# ── write-time guard ─────────────────────────────────────────────────────────
def test_upsert_marketing_template_unmapped_is_refused(env_guard):
    c = _make_app(NOTIFY_CONSENT_PURPOSES=json.dumps({"promo_x": "marketing"}))
    # 'promo_y' is marketing but NOT in the purposes map → refused.
    r = c.post("/api/v1/templates",
               json={"key": "promo_y", "locale": "en", "subject": "s", "body": "b",
                     "category": "marketing"}, headers=_admin())
    assert r.status_code == 400


def test_upsert_marketing_template_mapped_is_allowed(env_guard):
    c = _make_app(NOTIFY_CONSENT_PURPOSES=json.dumps({"promo_x": "marketing"}))
    r = c.post("/api/v1/templates",
               json={"key": "promo_x", "locale": "en", "subject": "s", "body": "b",
                     "category": "marketing"}, headers=_admin())
    assert r.status_code == 200 and r.json()["category"] == "marketing"


def test_upsert_transactional_template_needs_no_mapping(env_guard):
    c = _make_app()   # no purposes configured
    r = c.post("/api/v1/templates",
               json={"key": "otp", "locale": "en", "subject": "s", "body": "b"},
               headers=_admin())
    assert r.status_code == 200 and r.json()["category"] == "transactional"


# ── boot guard ───────────────────────────────────────────────────────────────
def test_boot_refuses_an_unmapped_marketing_seed(env_guard):
    seeds = [{"key": "promo_boot", "locale": "en", "subject": "s", "body": "b",
              "category": "marketing"}]
    with pytest.raises(Exception):   # RuntimeError from the lifespan boot guard
        with _make_app(NOTIFY_SEED_TEMPLATES=json.dumps(seeds)):   # no matching purpose
            pass


def test_boot_accepts_a_mapped_marketing_seed(env_guard):
    seeds = [{"key": "promo_boot", "locale": "en", "subject": "s", "body": "b",
              "category": "marketing"}]
    with _make_app(NOTIFY_SEED_TEMPLATES=json.dumps(seeds),
                   NOTIFY_CONSENT_PURPOSES=json.dumps({"promo_boot": "marketing"})) as c:
        assert c.get("/healthz").status_code in (200, 204)
