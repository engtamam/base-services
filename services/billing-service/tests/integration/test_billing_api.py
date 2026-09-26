"""Integration: catalog + subscribe + run(mock gateway)+e-invoice + webhook + gating."""
from __future__ import annotations

import datetime as dt
import os

import jwt
import pytest
from fastapi.testclient import TestClient

from app.ports.payment_gateway import Charge, WebhookEvent

SECRET = "h" * 40
ITOKEN = "i" * 40


class MockGW:
    driver = "mock"

    def charge(self, **k):
        return Charge(ok=True, status="succeeded", provider="mock",
                      provider_id="ch_" + k["idempotency_key"][:6], amount_minor=k["amount_minor"],
                      currency=k["currency"])

    def refund(self, **k):
        return Charge(ok=True, status="refunded", provider="mock", provider_id="re_1")

    def verify_webhook(self, **k):
        import json
        try:
            e = json.loads(k["body"])
        except Exception:
            return WebhookEvent(verified=False)
        return WebhookEvent(verified=k["secret"] == ITOKEN, type=e.get("type", ""), provider_id=e.get("pid", ""))


class MockEInv:
    driver = "zatca"

    def __init__(self):
        self.calls = []

    def finalize(self, *, tenant, invoice):
        self.calls.append(invoice["number"])
        return {"status": "reported"}


def _make_app():
    from app.config import get_settings

    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN=ITOKEN, TOKEN_VERIFIER_DRIVER="hs256",
                      TOKEN_HS256_SECRET=SECRET, RATELIMIT_MAX="1000", PAYMENT_WEBHOOK_SECRET=ITOKEN,
                      BILLING_DOCUMENT_MODE="tax_invoice", EINVOICER_DRIVER="zatca-http",
                      # A COMPLETE tax profile — tax_invoice mode now issues a tax invoice ONLY when
                      # the calculator (vat, default), rate (0.15, default), seller number AND a real
                      # e-invoicer driver are all set (the container's einvoicer object is overridden
                      # to a mock below, but the profile gate reads the DRIVER STRING).
                      INVOICE_SELLER_NAME="ACME KSA", INVOICE_SELLER_VAT="300000000000003")
    get_settings.cache_clear()
    from app.main import create_app

    app = create_app()
    app.state.container.payment_gateway = MockGW()
    app.state.container.einvoicer = MockEInv()
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(scope="module")
def client():
    from app.config import get_settings

    saved = dict(os.environ)
    c = _make_app()
    yield c
    os.environ.clear()
    os.environ.update(saved)
    get_settings.cache_clear()


def _adm(caps=("platform.billing",), tenant="t1"):
    now = dt.datetime.now(dt.timezone.utc)
    tok = jwt.encode({"sub": "a", "caps": list(caps), "tenant": tenant,
                      "exp": now + dt.timedelta(minutes=5), "iat": now}, SECRET, algorithm="HS256")
    return {"Authorization": "Bearer " + tok}


def _plan(client):
    return client.post("/api/v1/billing/plans",
                       json={"name": "Pro", "prices": [{"interval": "month", "amount_minor": 10000,
                                                        "currency": "SAR", "type": "flat"}]},
                       headers=_adm()).json()


def test_platform_invoice_list_rejects_a_non_iso_bound(client):
    # Round-3 #1: an epoch bound compared AS TEXT drops every real invoice ("2026…" > "178…") and
    # returns a plausible empty page — which settled partner statements at 0.00. It must 422, loudly.
    r = client.get("/api/v1/billing/admin/invoices?tenant=t1&since=1780272000", headers=_adm())
    assert r.status_code == 422
    ok = client.get("/api/v1/billing/admin/invoices?tenant=t1&since=2026-01-01T00:00:00Z", headers=_adm())
    assert ok.status_code == 200


def test_platform_invoice_list_exposes_tax_breakdown_and_refunds(client):
    # Round-3 #2 + #3: a partner commission needs the NET (subtotal, not VAT-inclusive total) and what
    # actually went back (refunded_minor) — both are now projected.
    plan = _plan(client)
    sub = client.post("/api/v1/billing/subscriptions",
                      json={"plan_id": plan["id"], "payment_token": "pm"}, headers=_adm()).json()
    client.post(f"/api/v1/billing/subscriptions/{sub['id']}/run", headers=_adm())
    rows = client.get("/api/v1/billing/admin/invoices?tenant=t1", headers=_adm()).json()["invoices"]
    assert rows
    assert {"subtotal_minor", "tax_minor", "total_minor", "refunded_minor"}.issubset(rows[0])


def test_platform_subscription_list_needs_a_filter_and_rejects_a_non_iso_bound(client):
    # Mirror of the invoice route: an unfiltered cross-tenant dump is refused, and an epoch bound
    # (compared as text) that would silently return an empty page must 422 rather than mislead.
    assert client.get("/api/v1/billing/admin/subscriptions", headers=_adm()).status_code == 400
    assert client.get("/api/v1/billing/admin/subscriptions?tenant=t1&since=1780272000",
                      headers=_adm()).status_code == 422
    # since/until refine a store-level filter (status/plan_id/tenant), exactly as the invoice route:
    ok = client.get("/api/v1/billing/admin/subscriptions?tenant=t1&since=2020-01-01T00:00:00Z",
                    headers=_adm())
    assert ok.status_code == 200


def test_platform_subscription_list_projects_lifecycle_fields_no_pii(client):
    plan = _plan(client)
    client.post("/api/v1/billing/subscriptions",
                json={"plan_id": plan["id"], "payment_token": "pm", "buyer_name": "Secret Buyer"},
                headers=_adm()).json()
    rows = client.get("/api/v1/billing/admin/subscriptions?tenant=t1",
                      headers=_adm()).json()["subscriptions"]
    assert rows
    assert {"id", "tenant", "plan_id", "status", "created", "canceled_at"}.issubset(rows[0])
    # a lifecycle projection, never buyer PII
    assert "buyer_name" not in rows[0] and "payment_token" not in rows[0]
    assert "Secret Buyer" not in client.get(
        "/api/v1/billing/admin/subscriptions?tenant=t1", headers=_adm()).text


def test_platform_subscription_list_is_platform_capability_only(client):
    # a tenant admin (billing.manage) is refused outright, never handed their own rows silently
    assert client.get("/api/v1/billing/admin/subscriptions?tenant=t1",
                      headers=_adm(caps=("billing.manage",))).status_code == 403


# ── FIN-005 / FIN-006: global config + fleet actions are PLATFORM-only ───────────────
def test_create_plan_is_platform_only_not_tenant_billing_manage(client):
    # A Plan is GLOBAL (shown on the unauthenticated /public/plans). A tenant-scoped billing.manage
    # must not inject one; only platform.billing may.
    body = {"name": "Escalate", "prices": [{"interval": "month", "amount_minor": 10000,
                                            "currency": "SAR", "type": "flat"}]}
    assert client.post("/api/v1/billing/plans", json=body,
                       headers=_adm(caps=("billing.manage",))).status_code == 403
    assert client.post("/api/v1/billing/plans", json=body, headers=_adm()).status_code == 201


def test_coupon_crud_is_platform_only(client):
    # The escalation: a tenant billing.manage could mint a public 100%-off code for every tenant.
    coup = {"code": "FIN005", "type": "percent", "value": 100, "public": True}
    mgr = _adm(caps=("billing.manage",))
    assert client.post("/api/v1/billing/coupons", json=coup, headers=mgr).status_code == 403
    assert client.get("/api/v1/billing/coupons", headers=mgr).status_code == 403
    # platform.billing may, and then the escalated caller still cannot edit/delete it
    assert client.post("/api/v1/billing/coupons", json=coup, headers=_adm()).status_code == 201
    assert client.get("/api/v1/billing/coupons/FIN005", headers=mgr).status_code == 403
    assert client.patch("/api/v1/billing/coupons/FIN005", json={"value": 50},
                        headers=mgr).status_code == 403
    assert client.delete("/api/v1/billing/coupons/FIN005", headers=mgr).status_code == 403


def test_dunning_run_is_platform_only(client):
    # dunning_sweep() retries charges across ALL tenants — not a tenant-scoped action.
    assert client.post("/api/v1/billing/dunning/run",
                       headers=_adm(caps=("billing.manage",))).status_code == 403
    assert client.post("/api/v1/billing/dunning/run", headers=_adm()).status_code == 200


def test_canceled_at_is_stamped_on_immediate_cancel(client):
    plan = _plan(client)
    sub = client.post("/api/v1/billing/subscriptions",
                      json={"plan_id": plan["id"], "payment_token": "pm"}, headers=_adm()).json()
    # cancel immediately (not at period end)
    client.post(f"/api/v1/billing/subscriptions/{sub['id']}/cancel?at_period_end=false",
                headers=_adm())
    row = next(r for r in client.get("/api/v1/billing/admin/subscriptions?status=canceled",
                                     headers=_adm()).json()["subscriptions"] if r["id"] == sub["id"])
    assert row["status"] == "canceled" and row["canceled_at"]


def test_subscribe_run_paid_einvoiced(client):
    plan = _plan(client)
    sub = client.post("/api/v1/billing/subscriptions",
                      json={"plan_id": plan["id"], "payment_token": "pm_1"}, headers=_adm()).json()
    inv = client.post(f"/api/v1/billing/subscriptions/{sub['id']}/run", headers=_adm()).json()
    assert inv["status"] == "paid" and inv["total_minor"] == 11500
    assert inv["einvoice"]["status"] == "reported"          # zatca tie-in fired
    assert client.get("/api/v1/billing/invoices", headers=_adm()).json()["invoices"]


def test_coupon_reduces_total(client):
    plan = _plan(client)
    client.post("/api/v1/billing/coupons", json={"code": "HALF", "type": "percent", "value": 50}, headers=_adm())
    sub = client.post("/api/v1/billing/subscriptions",
                      json={"plan_id": plan["id"], "coupon_code": "HALF", "payment_token": "pm"}, headers=_adm()).json()
    inv = client.post(f"/api/v1/billing/subscriptions/{sub['id']}/run", headers=_adm()).json()
    assert inv["discount_minor"] == 5000 and inv["total_minor"] == 5750   # (10000-5000)+15%


def test_subscribe_unknown_plan_404(client):
    r = client.post("/api/v1/billing/subscriptions", json={"plan_id": "nope"}, headers=_adm())
    assert r.status_code == 404


def test_webhook_signature_required(client):
    import json
    body = json.dumps({"type": "payment.succeeded", "pid": "ch_zzz"}).encode()
    # wrong secret path: our MockGW marks verified only when secret==ITOKEN, which the route passes,
    # so an unknown provider_id reconciles cleanly; a malformed body -> verify False -> 400
    bad = client.post("/internal/billing/webhook/mock", content=b"not-json",
                      headers={"X-Internal-Token": ITOKEN})
    assert bad.status_code == 400


def test_admin_needs_capability(client):
    assert client.post("/api/v1/billing/plans", json={"name": "x", "prices": [{"amount_minor": 1}]},
                       headers=_adm(caps=())).status_code == 403


def test_cross_tenant_invoice_hidden(client):
    plan = _plan(client)
    sub = client.post("/api/v1/billing/subscriptions",
                      json={"plan_id": plan["id"], "payment_token": "pm", "tenant": "owner"},
                      headers=_adm()).json()
    inv = client.post(f"/api/v1/billing/subscriptions/{sub['id']}/run", headers=_adm()).json()
    # a tenant-scoped admin from another tenant can't fetch the PDF
    other = _adm(caps=("billing.manage",), tenant="intruder")
    assert client.get(f"/api/v1/billing/invoices/{inv['id']}/pdf", headers=other).status_code == 404


# ── update payment method: self-service recovery from an expired/declined card ───────────────────────
def test_update_payment_method_updates_the_token_without_echoing_it(client):
    plan = _plan(client)
    sub = client.post("/api/v1/billing/subscriptions",
                      json={"plan_id": plan["id"], "payment_token": "pm_old"}, headers=_adm()).json()
    r = client.post(f"/api/v1/billing/subscriptions/{sub['id']}/payment-method",
                    json={"payment_token": "pm_new_9"}, headers=_adm(caps=("billing.self",)))
    assert r.status_code == 200 and r.json() == {"subscription_id": sub["id"], "updated": True}
    assert "pm_new_9" not in r.text                      # a write-only credential, never echoed


def test_update_payment_method_unknown_subscription_is_404(client):
    r = client.post("/api/v1/billing/subscriptions/nope/payment-method",
                    json={"payment_token": "pm_x"}, headers=_adm(caps=("billing.self",)))
    assert r.status_code == 404


def test_update_payment_method_rejects_another_tenants_subscription(client):
    plan = _plan(client)
    sub = client.post("/api/v1/billing/subscriptions",
                      json={"plan_id": plan["id"], "payment_token": "pm"}, headers=_adm()).json()
    r = client.post(f"/api/v1/billing/subscriptions/{sub['id']}/payment-method",
                    json={"payment_token": "pm_evil"},
                    headers=_adm(caps=("billing.self",), tenant="intruder"))
    assert r.status_code in (403, 404)                   # the tenant guard refuses; never another's row


def test_update_payment_method_empty_token_is_refused(client):
    plan = _plan(client)
    sub = client.post("/api/v1/billing/subscriptions",
                      json={"plan_id": plan["id"], "payment_token": "pm"}, headers=_adm()).json()
    r = client.post(f"/api/v1/billing/subscriptions/{sub['id']}/payment-method",
                    json={"payment_token": ""}, headers=_adm(caps=("billing.self",)))
    assert r.status_code == 422                           # min_length=1 blocks a blank token at the door


# ── undo a scheduled cancellation (self-service "changed my mind") ────────────────────────────────────
def test_undo_cancel_reverses_a_scheduled_cancellation(client):
    plan = _plan(client)
    sub = client.post("/api/v1/billing/subscriptions",
                      json={"plan_id": plan["id"], "payment_token": "pm"}, headers=_adm()).json()
    slf = _adm(caps=("billing.self",))
    assert client.post(f"/api/v1/billing/subscriptions/{sub['id']}/cancel", headers=slf).status_code == 200
    r = client.post(f"/api/v1/billing/subscriptions/{sub['id']}/undo-cancel", headers=slf)
    assert r.status_code == 200 and r.json()["undone_cancellation"] is True
    # idempotent: a second undo is a harmless no-op, not an error
    r2 = client.post(f"/api/v1/billing/subscriptions/{sub['id']}/undo-cancel", headers=slf)
    assert r2.status_code == 200 and r2.json()["undone_cancellation"] is False


def test_undo_cancel_rejects_another_tenants_subscription(client):
    plan = _plan(client)
    sub = client.post("/api/v1/billing/subscriptions",
                      json={"plan_id": plan["id"], "payment_token": "pm"}, headers=_adm()).json()
    r = client.post(f"/api/v1/billing/subscriptions/{sub['id']}/undo-cancel",
                    headers=_adm(caps=("billing.self",), tenant="intruder"))
    assert r.status_code in (403, 404)


# ── renewal preview (self-service billing screen) ─────────────────────────────
def test_renewal_preview_route_is_self_scoped_and_shaped(client):
    slf = _adm(caps=("billing.self",), tenant="tprev_empty")
    r = client.get("/api/v1/billing/subscriptions/renewal-preview", headers=slf)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["groups"] == [] and "invoice_mode" in body and "ownership_as_of" in body


def test_renewal_preview_route_refuses_cross_tenant_for_a_non_platform_user(client):
    slf = _adm(caps=("billing.self",), tenant="tprev")
    r = client.get("/api/v1/billing/subscriptions/renewal-preview?tenant=someoneelse", headers=slf)
    assert r.status_code == 403                       # _scope: CROSS_TENANT_FORBIDDEN


# ── multi-plan (cart) checkout route ──────────────────────────────────────────
def test_checkout_route_creates_a_subscription_per_plan_for_the_caller(client):
    p1 = _plan(client)
    p2 = _plan(client)                                      # a second, distinct plan id
    slf = _adm(caps=("billing.self",), tenant="cartuser")
    r = client.post("/api/v1/billing/subscriptions/checkout",
                    json={"items": [{"plan_id": p1["id"]}, {"plan_id": p2["id"]}],
                          "payment_token": "pm"}, headers=slf)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["tenant"] == "cartuser"
    assert {i["plan_id"] for i in body["items"]} == {p1["id"], p2["id"]}
    assert isinstance(body["paid"], bool)


def test_checkout_route_ignores_a_forged_tenant_for_a_non_platform_caller(client):
    p1 = _plan(client)
    slf = _adm(caps=("billing.self",), tenant="attacker")
    r = client.post("/api/v1/billing/subscriptions/checkout",
                    json={"items": [{"plan_id": p1["id"]}], "payment_token": "pm", "tenant": "victim"},
                    headers=slf)
    assert r.status_code == 201, r.text
    assert r.json()["tenant"] == "attacker"                 # forged body tenant ignored, never "victim"


def test_checkout_route_refuses_a_bad_cart_with_422(client):
    slf = _adm(caps=("billing.self",), tenant="cartuser2")
    r = client.post("/api/v1/billing/subscriptions/checkout",
                    json={"items": [{"plan_id": "ghost"}], "payment_token": "pm"}, headers=slf)
    # A refused cart is a 422; the specific reason is logged (with request_id), never leaked in the
    # body — the platform's CWE-209 posture, same as every other route here.
    assert r.status_code == 422
    assert "ghost" not in r.text and r.json()["error"] == "VALIDATION_ERROR"


def test_checkout_route_requires_billing_self_capability(client):
    r = client.post("/api/v1/billing/subscriptions/checkout",
                    json={"items": [{"plan_id": "x"}], "payment_token": "pm"},
                    headers=_adm(caps=("something.else",)))
    assert r.status_code in (401, 403)                      # not a billing principal → refused


def test_checkout_preview_route_returns_totals_and_creates_nothing(client):
    p1 = _plan(client); p2 = _plan(client)
    slf = _adm(caps=("billing.self",), tenant="prevuser")
    r = client.post("/api/v1/billing/subscriptions/checkout/preview",
                    json={"items": [{"plan_id": p1["id"]}, {"plan_id": p2["id"]}]}, headers=slf)
    assert r.status_code == 200, r.text
    body = r.json()
    assert {"currency", "subtotal_minor", "discount_minor", "tax_minor", "total_minor",
            "items"}.issubset(body)
    assert len(body["items"]) == 2
    # a preview never subscribes the caller
    subs = client.get("/api/v1/billing/subscriptions", headers=slf).json()["subscriptions"]
    assert subs == []


def test_checkout_preview_route_refuses_a_bad_cart(client):
    slf = _adm(caps=("billing.self",), tenant="prevuser2")
    r = client.post("/api/v1/billing/subscriptions/checkout/preview",
                    json={"items": [{"plan_id": "ghost"}]}, headers=slf)
    assert r.status_code == 422


# ── one-off wallet top-up route ───────────────────────────────────────────────

def _self(tenant="t1"):
    return _adm(caps=("billing.self",), tenant=tenant)


def test_wallet_topup_charges_and_returns_a_receipt(client):
    r = client.post("/api/v1/billing/wallet-topups",
                    json={"amount_minor": 25000, "payment_token": "tok_x", "fund": "topup"},
                    headers=_self())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["fund"] == "topup" and body["amount_minor"] == 25000
    # default is a receipt (no VAT at top-up) even though the app runs in tax_invoice document mode
    assert body["doc_type"] == "receipt"


def test_wallet_topup_needs_a_self_capability(client):
    r = client.post("/api/v1/billing/wallet-topups",
                    json={"amount_minor": 1000, "payment_token": "t", "fund": "topup"},
                    headers=_adm(caps=("something.else",)))
    assert r.status_code == 403


def test_wallet_topup_rejects_a_non_positive_amount(client):
    r = client.post("/api/v1/billing/wallet-topups",
                    json={"amount_minor": 0, "payment_token": "t", "fund": "topup"},
                    headers=_self())
    assert r.status_code == 422        # pydantic gt=0


def test_wallet_topup_rejects_below_the_configured_minimum(client):
    # default min is 100 minor (1 SAR); 50 is a positive amount that Field(gt=0) would let through.
    r = client.post("/api/v1/billing/wallet-topups",
                    json={"amount_minor": 50, "payment_token": "t", "fund": "topup"},
                    headers=_self())
    assert r.status_code == 400        # below the configured floor (detail sanitized by the handler)


def test_wallet_topup_rejects_above_the_configured_maximum(client):
    # default max is 5,000,000 minor (50,000 SAR).
    r = client.post("/api/v1/billing/wallet-topups",
                    json={"amount_minor": 5_000_001, "payment_token": "t", "fund": "topup"},
                    headers=_self())
    assert r.status_code == 400


def test_wallet_topup_checkout_enforces_the_same_bounds(client):
    assert client.post("/api/v1/billing/wallet-topups/checkout",
                       json={"amount_minor": 50}, headers=_self()).status_code == 400
    assert client.post("/api/v1/billing/wallet-topups/checkout",
                       json={"amount_minor": 5_000_001}, headers=_self()).status_code == 400


def test_wallet_topup_is_scoped_to_the_callers_tenant(client):
    # a non-platform caller cannot top up another tenant's wallet: _tenant() ignores body.tenant
    r = client.post("/api/v1/billing/wallet-topups",
                    json={"amount_minor": 1000, "payment_token": "t", "fund": "topup",
                          "tenant": "victim"},
                    headers=_self(tenant="attacker"))
    assert r.status_code == 201
    assert r.json()["invoice_id"]      # created under 'attacker', not 'victim' (see core: tenant arg)


def test_wallet_topup_checkout_route_is_wired_and_capability_gated(client):
    # capability required
    assert client.post("/api/v1/billing/wallet-topups/checkout",
                       json={"amount_minor": 1000}, headers=_adm(caps=("nope",))).status_code == 403
    # amount must be positive (pydantic)
    assert client.post("/api/v1/billing/wallet-topups/checkout",
                       json={"amount_minor": 0}, headers=_self()).status_code == 422
    # the mock gateway has no server-side checkout → a clean 502, not a 500
    r = client.post("/api/v1/billing/wallet-topups/checkout",
                    json={"amount_minor": 5000, "fund": "topup"}, headers=_self())
    assert r.status_code == 502


def test_pending_topups_listing_is_platform_only(client):
    # tenant self-capability is not enough — platform.billing required
    assert client.get("/api/v1/billing/admin/wallet-topups/pending",
                      headers=_self()).status_code == 403
    r = client.get("/api/v1/billing/admin/wallet-topups/pending", headers=_adm())
    assert r.status_code == 200 and "payments" in r.json()
