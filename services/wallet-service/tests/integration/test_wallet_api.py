"""Wallet HTTP surface.

The security shape: money moves ONLY on the internal tier. A customer who could credit their
own wallet would print money, so `/api/v1/wallet` is read-only by construction — there is no
mutating user route to protect.
"""
from __future__ import annotations

import os
import time

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "s" * 40
INTERNAL = "t" * 40


@pytest.fixture(scope="module")
def client():
    from app.config import get_settings

    saved = dict(os.environ)
    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN=INTERNAL, TOKEN_VERIFIER_DRIVER="hs256",
                      TOKEN_HS256_SECRET=SECRET, TOKEN_ISSUER="i", TOKEN_AUDIENCE="a",
                      RATELIMIT_MAX="100000")
    get_settings.cache_clear()
    try:
        from app.main import create_app

        with TestClient(create_app()) as c:
            yield c
    finally:
        os.environ.clear()
        os.environ.update(saved)
        get_settings.cache_clear()


def tok(caps, tenant="t1"):
    claims = {"sub": "u1", "caps": caps, "tenant": tenant, "iss": "i", "aud": "a",
              "exp": int(time.time()) + 600}
    return {"Authorization": "Bearer " + jwt.encode(claims, SECRET, algorithm="HS256")}


IT = {"X-Internal-Token": INTERNAL}


def credit(client, tenant, fund, amount, source="", **kw):
    return client.post("/internal/wallet/credit",
                       json={"tenant": tenant, "fund": fund, "amount": amount,
                             "source": source, **kw}, headers=IT)


# ── nobody credits their own wallet ─────────────────────────────────────────
def test_the_user_tier_has_no_mutating_route(client):
    r = client.post("/api/v1/wallet/credit", json={"fund": "cash", "amount": 10_000},
                    headers=tok(["platform.wallet"]))
    assert r.status_code in (404, 405)


def test_money_movement_requires_the_internal_token(client):
    for path in ("credit", "spend", "reserve", "confirm", "release", "refund"):
        assert client.post(f"/internal/wallet/{path}", json={}).status_code == 401


def test_a_user_token_cannot_reach_the_internal_tier(client):
    assert credit(client, "t1", "cash", 10, "sanity").status_code == 200   # the internal token works
    r = client.post("/internal/wallet/credit",
                    json={"tenant": "t1", "fund": "cash", "amount": 10},
                    headers=tok(["platform.wallet"]))
    assert r.status_code == 401, "a user JWT must never move money"


def test_reading_a_balance_needs_a_capability(client):
    assert client.get("/api/v1/wallet").status_code == 401
    assert client.get("/api/v1/wallet", headers=tok(["unrelated"])).status_code == 403
    assert client.get("/api/v1/wallet", headers=tok(["wallet.read"])).status_code == 200


def test_a_tenant_reads_only_its_own_wallet(client):
    credit(client, "victim", "cash", 5_000, "v1")
    body = client.get("/api/v1/wallet", headers=tok(["wallet.read"], tenant="attacker")).json()
    assert body["tenant"] == "attacker" and body["balance"] == 0


# ── operator: cross-tenant reads, platform-only, money never moves ────────────
def test_operator_reads_are_platform_only_not_a_tenant_capability(client):
    """The one wall between companies: a tenant user who can read its OWN wallet must NOT be able to
    name and read another tenant's. The /admin reads require platform.wallet, never wallet.read."""
    for path in ("/api/v1/wallet/admin/balance?tenant=victim",
                 "/api/v1/wallet/admin/ledger?tenant=victim",
                 "/api/v1/wallet/admin/reconcile?tenant=victim"):
        assert client.get(path, headers=tok(["wallet.read"])).status_code == 403, path
        assert client.get(path, headers=tok(["wallet.manage"])).status_code == 403, path
        assert client.get(path).status_code == 401, path


def test_an_operator_reads_any_tenants_wallet(client):
    credit(client, "cust_a", "cash", 7_000, "c")
    # The operator's own token tenant is 'ops', but it reads cust_a's wallet explicitly.
    body = client.get("/api/v1/wallet/admin/balance?tenant=cust_a",
                      headers=tok(["platform.wallet"], tenant="ops")).json()
    assert body["tenant"] == "cust_a" and body["balance"] == 7_000


def test_the_operator_tenant_argument_is_mandatory(client):
    r = client.get("/api/v1/wallet/admin/balance", headers=tok(["platform.wallet"]))
    assert r.status_code == 422, "a cross-tenant read with no tenant must be refused, not defaulted"


def test_operator_reconcile_proves_consistency_without_moving_money(client):
    credit(client, "cust_r", "cash", 9_000, "c")
    client.post("/internal/wallet/spend", json={"tenant": "cust_r", "net": 200, "source": "s"},
                headers=IT)
    before = client.get("/internal/wallet/cust_r", headers=IT).json()["balance"]
    out = client.get("/api/v1/wallet/admin/reconcile?tenant=cust_r",
                     headers=tok(["platform.wallet"], tenant="ops")).json()
    assert out["consistent"] is True
    # Read-only: reconciling did not move a cent.
    assert client.get("/internal/wallet/cust_r", headers=IT).json()["balance"] == before


def test_internal_ledger_lists_a_tenants_rows_read_only(client):
    # The internal-token twin of /admin/ledger, for the cross-repo dispute-defense report.
    credit(client, "cust_led", "cash", 9_000, "topup")
    client.post("/internal/wallet/spend", json={"tenant": "cust_led", "net": 200, "source": "sms"},
                headers=IT)
    before = client.get("/internal/wallet/cust_led", headers=IT).json()["balance"]
    r = client.get("/internal/wallet/cust_led/ledger", headers=IT)
    assert r.status_code == 200
    body = r.json()
    assert body["tenant"] == "cust_led" and body["count"] == len(body["entries"])
    kinds = {e["type"] for e in body["entries"]}
    assert kinds == {"credit", "spend"}                        # both rows, full detail per row
    # Reading the statement moved nothing.
    assert client.get("/internal/wallet/cust_led", headers=IT).json()["balance"] == before


def test_internal_ledger_requires_the_internal_token(client):
    assert client.get("/internal/wallet/anyone/ledger").status_code == 401


# ── money ───────────────────────────────────────────────────────────────────
def test_credit_then_spend_applies_promo_before_vat(client):
    credit(client, "t_spend", "cash", 10_000, "c1")
    credit(client, "t_spend", "promo", 500, "g1")
    r = client.post("/internal/wallet/spend",
                    json={"tenant": "t_spend", "net": 1000, "source": "op1"}, headers=IT).json()
    assert r["plan"]["discount"] == 500 and r["plan"]["vat"] == 75
    assert r["plan"]["applied"] == {"promo": 500, "cash": 575}


def test_overdrawing_is_402_and_changes_nothing(client):
    credit(client, "t_over", "cash", 100, "c")
    r = client.post("/internal/wallet/spend",
                    json={"tenant": "t_over", "net": 999_999, "source": "x"}, headers=IT)
    assert r.status_code == 402
    assert client.get("/internal/wallet/t_over", headers=IT).json()["balance"] == 100


def test_an_unknown_fund_is_rejected(client):
    assert credit(client, "t1", "bitcoin", 10, "x").status_code == 400


def test_a_negative_amount_is_rejected_by_validation(client):
    assert credit(client, "t1", "cash", -5, "x").status_code == 422


def test_a_redelivered_settlement_credits_once(client):
    credit(client, "t_idem", "cash", 10_000, "psp-evt-9")
    credit(client, "t_idem", "cash", 10_000, "psp-evt-9")
    assert client.get("/internal/wallet/t_idem", headers=IT).json()["balance"] == 10_000


# ── holds ───────────────────────────────────────────────────────────────────
def test_reserve_holds_confirm_debits_and_replay_is_free(client):
    credit(client, "t_hold", "cash", 1_000, "c")
    h = client.post("/internal/wallet/reserve",
                    json={"tenant": "t_hold", "net": 100, "source": "r"}, headers=IT).json()

    b = client.get("/internal/wallet/t_hold", headers=IT).json()
    assert b["balance"] == 1_000 and b["available_total"] == 885

    assert client.post("/internal/wallet/confirm", json={"hold_id": h["hold_id"]},
                       headers=IT).json()["state"] == "confirmed"
    again = client.post("/internal/wallet/confirm", json={"hold_id": h["hold_id"]},
                        headers=IT).json()
    assert again["idempotent"] is True
    assert client.get("/internal/wallet/t_hold", headers=IT).json()["balance"] == 885


def test_releasing_a_hold_frees_the_money(client):
    credit(client, "t_rel", "cash", 1_000, "c")
    h = client.post("/internal/wallet/reserve",
                    json={"tenant": "t_rel", "net": 100, "source": "r"}, headers=IT).json()
    client.post("/internal/wallet/release", json={"hold_id": h["hold_id"]}, headers=IT)
    b = client.get("/internal/wallet/t_rel", headers=IT).json()
    assert b["balance"] == 1_000 and b["held"] == {}


def test_confirming_an_unknown_hold_is_a_400(client):
    r = client.post("/internal/wallet/confirm", json={"hold_id": "hld_nope"}, headers=IT)
    assert r.status_code == 400


def test_sweep_endpoints_are_callable_and_bounded(client):
    assert client.post("/internal/wallet/sweep-holds", headers=IT).status_code == 200
    assert client.post("/internal/wallet/sweep-grants", headers=IT).status_code == 200


# ── refund ──────────────────────────────────────────────────────────────────
def test_a_refund_returns_money_to_the_funds_it_came_from(client):
    credit(client, "t_ref", "promo", 400, "g")
    credit(client, "t_ref", "cash", 5_000, "c")
    spend = client.post("/internal/wallet/spend",
                        json={"tenant": "t_ref", "net": 1000, "source": "op"},
                        headers=IT).json()
    seq = spend["entry"]["seq"]
    out = client.post("/internal/wallet/refund",
                      json={"tenant": "t_ref", "seq": seq, "source": "rf"}, headers=IT).json()
    assert out["funds"] == {"promo": 400, "cash": 5_000}


def test_refunding_a_credit_is_refused(client):
    r = credit(client, "t_rc", "cash", 100, "c").json()
    out = client.post("/internal/wallet/refund",
                      json={"tenant": "t_rc", "seq": r["entry"]["seq"], "source": "x"},
                      headers=IT)
    assert out.status_code == 400


# ── invariants ──────────────────────────────────────────────────────────────
def test_reconcile_proves_the_balance_equals_the_ledger(client):
    credit(client, "t_rec", "cash", 10_000, "c")
    client.post("/internal/wallet/spend", json={"tenant": "t_rec", "net": 100, "source": "s"},
                headers=IT)
    out = client.get("/internal/wallet/t_rec/reconcile", headers=IT).json()
    assert out["consistent"] is True and out["stored"] == out["derived"]


def test_the_ledger_is_paginated_and_bounded(client):
    credit(client, "t_led", "cash", 10_000, "c")
    for i in range(5):
        client.post("/internal/wallet/spend",
                    json={"tenant": "t_led", "net": 10, "source": f"s{i}"}, headers=IT)
    page = client.get("/api/v1/wallet/ledger?limit=2",
                      headers=tok(["wallet.read"], tenant="t_led")).json()
    assert page["count"] == 2 and page["next_before_seq"] == page["entries"][-1]["seq"]


def test_manifest_advertises_the_optional_events(client):
    m = client.get("/internal/manifest", headers=IT).json()
    assert set(m["capabilities"]) == {"platform.wallet", "wallet.manage", "wallet.read"}
    # billing invoices these IF it listens; the wallet works standalone.
    from app.core.events import EMITTED
    # The manifest is the ONLY place a consumer can discover these; it must equal what the code
    # declares, or an event fires that nobody can subscribe to.
    assert set(m["events"]["emitted"]) == set(EMITTED)
    assert {"port": "clock", "driver": "system"} in m["ports"]
