"""Audit FIN-007 — the wallet PAYOUT routes move money OUT of the platform and had NO integration
test. The route layer (auth, the mandatory idempotency source, error mapping, the reversal) is exactly
where a wiring bug is a direct financial loss, so it is driven here end-to-end over HTTP.

Routes: POST /internal/wallet/withdraw, /internal/wallet/withdrawals/settle, /internal/wallet/reclaim.
"""
from __future__ import annotations

import os
import time

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "s" * 40
INTERNAL = "t" * 40
IT = {"X-Internal-Token": INTERNAL}


@pytest.fixture()
def client():
    from app.config import get_settings
    saved = dict(os.environ)
    # a WITHDRAWABLE fund (4th field) — the default config has none, so a payout would refuse.
    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN=INTERNAL, TOKEN_VERIFIER_DRIVER="hs256",
                      TOKEN_HS256_SECRET=SECRET, TOKEN_ISSUER="i", TOKEN_AUDIENCE="a",
                      RATELIMIT_MAX="100000", WALLET_FUNDS="cash:1:taxed:withdrawable")
    get_settings.cache_clear()
    try:
        from app.main import create_app
        with TestClient(create_app()) as c:
            yield c
    finally:
        os.environ.clear(); os.environ.update(saved)
        get_settings.cache_clear()


def tok(caps, tenant="t1"):
    claims = {"sub": "u1", "caps": caps, "tenant": tenant, "iss": "i", "aud": "a",
              "exp": int(time.time()) + 600}
    return {"Authorization": "Bearer " + jwt.encode(claims, SECRET, algorithm="HS256")}


def _credit(client, amount, source, tenant="t1"):
    r = client.post("/internal/wallet/credit",
                    json={"tenant": tenant, "fund": "cash", "amount": amount, "source": source},
                    headers=IT)
    assert r.status_code == 200, r.text


def _cash(client, tenant="t1"):
    return client.get("/api/v1/wallet", headers=tok(["wallet.read"], tenant)).json()["funds"].get("cash", 0)


# ── auth: money out is internal-only ────────────────────────────────────────────────
def test_payout_routes_require_the_internal_token(client):
    assert client.post("/internal/wallet/withdraw",
                       json={"tenant": "t1", "amount": 100, "source": "s"}).status_code == 401
    assert client.post("/internal/wallet/withdrawals/settle",
                       json={"withdrawal_id": "wd_x", "paid": True}).status_code == 401
    assert client.post("/internal/wallet/reclaim",
                       json={"grant_id": "grt_x"}).status_code == 401
    # a user JWT (even platform.wallet) must never move money out
    assert client.post("/internal/wallet/withdraw", json={"tenant": "t1", "amount": 100, "source": "s"},
                       headers=tok(["platform.wallet"])).status_code == 401


# ── the mandatory idempotency source (the double-pay guard) must survive the route ───
def test_withdraw_without_a_source_is_refused_400(client):
    _credit(client, 10_000, "seed")
    r = client.post("/internal/wallet/withdraw",
                    json={"tenant": "t1", "amount": 4000}, headers=IT)   # no source
    assert r.status_code == 400                                          # WITHDRAW_SOURCE_REQUIRED
    assert _cash(client) == 10_000                                       # nothing left the platform


# ── happy path: a payout debits withdrawable funds and records a pending withdrawal ──
def test_withdraw_debits_and_is_idempotent_by_source(client):
    _credit(client, 10_000, "seed")
    r = client.post("/internal/wallet/withdraw",
                    json={"tenant": "t1", "amount": 4000, "source": "w1", "destination": "iban:x"},
                    headers=IT)
    assert r.status_code == 200, r.text
    wd_id = r.json()["withdrawal_id"]
    assert r.json()["state"] == "pending"
    assert _cash(client) == 6000                                         # 4000 earmarked out

    # a retried payout with the SAME source must NOT debit again (idempotent) — the double-pay guard
    again = client.post("/internal/wallet/withdraw",
                        json={"tenant": "t1", "amount": 4000, "source": "w1", "destination": "iban:x"},
                        headers=IT)
    assert again.status_code == 200 and again.json()["withdrawal_id"] == wd_id
    assert _cash(client) == 6000                                         # still 6000, not 2000


def test_insufficient_withdrawable_balance_is_402(client):
    _credit(client, 1000, "seed")
    r = client.post("/internal/wallet/withdraw",
                    json={"tenant": "t1", "amount": 5000, "source": "w_big"}, headers=IT)
    assert r.status_code == 402 and _cash(client) == 1000               # refused, nothing moved


# ── settle: paid keeps the debit; failed REVERSES it (a transfer that never happened costs nothing) ─
def test_settle_paid_keeps_the_debit(client):
    _credit(client, 10_000, "seed")
    wd = client.post("/internal/wallet/withdraw",
                     json={"tenant": "t1", "amount": 4000, "source": "w1"}, headers=IT).json()
    r = client.post("/internal/wallet/withdrawals/settle",
                    json={"withdrawal_id": wd["withdrawal_id"], "paid": True}, headers=IT)
    assert r.status_code == 200 and r.json()["state"] == "paid"
    assert _cash(client) == 6000                                         # money stays out


def test_settle_failed_reverses_the_debit(client):
    _credit(client, 10_000, "seed")
    wd = client.post("/internal/wallet/withdraw",
                     json={"tenant": "t1", "amount": 4000, "source": "w1"}, headers=IT).json()
    assert _cash(client) == 6000
    r = client.post("/internal/wallet/withdrawals/settle",
                    json={"withdrawal_id": wd["withdrawal_id"], "paid": False}, headers=IT)
    assert r.status_code == 200 and r.json()["state"] == "failed"
    assert _cash(client) == 10_000                                       # the debit was reversed


# ── reclaim: route wiring + clean error mapping (no unmapped 500) ────────────────────
def test_reclaim_unknown_grant_is_400_not_500(client):
    r = client.post("/internal/wallet/reclaim", json={"grant_id": "grt_nope"}, headers=IT)
    assert r.status_code == 400                                          # GRANT_NOT_FOUND, mapped not 500
