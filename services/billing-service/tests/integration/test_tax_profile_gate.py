"""Integration: the operator write refuses a tax invoice from an incomplete profile — by name.

Acceptance #1 from the escalation, faithfully: a fresh platform (receipt mode, no seller number)
tries to flip BILLING_DOCUMENT_MODE to tax_invoice from the panel, and is refused with the missing
key named — TAX_PROFILE_INCOMPLETE: INVOICE_SELLER_VAT — never silently accepted.
"""
from __future__ import annotations

import datetime as dt
import os

import jwt
import pytest
from fastapi.testclient import TestClient

SECRET = "h" * 40
ITOKEN = "i" * 40


def _make_app():
    from app.config import get_settings
    # A fresh platform: receipt mode, NO seller registration. The default calculator (vat) and rate
    # (0.15) are present, so the seller number is the missing piece the refusal must name.
    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN=ITOKEN, TOKEN_VERIFIER_DRIVER="hs256",
                      TOKEN_HS256_SECRET=SECRET, RATELIMIT_MAX="1000",
                      SECRETS_KEK="0" * 64, PII_SALT="pepper", EINVOICER_DRIVER="zatca-http",
                      BILLING_DOCUMENT_MODE="receipt", INVOICE_SELLER_VAT="", INVOICE_SELLER_NAME="")
    get_settings.cache_clear()
    from app.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture()
def client():
    from app.config import get_settings
    saved = dict(os.environ)
    c = _make_app()
    yield c
    os.environ.clear(); os.environ.update(saved); get_settings.cache_clear()


def _cfg_admin(tenant="t1"):
    now = dt.datetime.now(dt.timezone.utc)
    tok = jwt.encode({"sub": "op", "caps": ["platform.config"], "tenant": tenant,
                      "exp": now + dt.timedelta(minutes=5), "iat": now}, SECRET, algorithm="HS256")
    return {"Authorization": "Bearer " + tok}


def _put(client, key, value, tenant=""):
    return client.put("/api/v1/billing/operator/config",
                      json={"key": key, "value": value, "tenant": tenant}, headers=_cfg_admin())


def test_enabling_tax_invoice_with_no_seller_vat_is_refused_by_name(client):
    r = _put(client, "BILLING_DOCUMENT_MODE", "tax_invoice")
    assert r.status_code == 400
    assert r.json()["error"] == "TAX_PROFILE_INCOMPLETE" and r.json()["missing_key"] == "INVOICE_SELLER_VAT"


def test_setting_the_seller_then_the_mode_is_accepted(client):
    # the whole profile is now editable AS ONE UNIT from the panel: set the seller (platform scope),
    # then the mode is allowed to become tax.
    assert _put(client, "INVOICE_SELLER_VAT", "300000000000003").status_code == 200
    assert _put(client, "BILLING_DOCUMENT_MODE", "tax_invoice").status_code == 200


def test_lowering_the_rate_to_zero_under_tax_mode_is_refused_by_name(client):
    _put(client, "INVOICE_SELLER_VAT", "300000000000003")
    assert _put(client, "BILLING_DOCUMENT_MODE", "tax_invoice").status_code == 200
    # the profile is atomic: a write to ANY key that breaks completeness is refused, named
    r = _put(client, "VAT_RATE", "0")
    assert r.status_code == 400
    assert r.json()["error"] == "TAX_PROFILE_INCOMPLETE" and r.json()["missing_key"] == "VAT_RATE"


def test_enabling_tax_invoice_with_a_null_einvoicer_is_refused_by_name(client):
    """The escalated gap: calculator, rate and seller number are all set, but EINVOICER_DRIVER is
    null — so the document would claim a ZATCA clearance that NullEInvoicer never performs. The write
    is refused up front, naming EINVOICER_DRIVER, instead of the next paid invoice silently degrading."""
    _put(client, "INVOICE_SELLER_VAT", "300000000000003")   # calculator + rate + seller all real
    _put(client, "EINVOICER_DRIVER", "null")                # ...but nothing to clear with ZATCA
    r = _put(client, "BILLING_DOCUMENT_MODE", "tax_invoice")
    assert r.status_code == 400
    assert r.json()["error"] == "TAX_PROFILE_INCOMPLETE" and r.json()["missing_key"] == "EINVOICER_DRIVER"


def test_nulling_the_einvoicer_under_tax_mode_is_refused(client):
    # atomic both ways: once tax mode is on, lowering the e-invoicer back to null is refused, named.
    _put(client, "INVOICE_SELLER_VAT", "300000000000003")
    assert _put(client, "BILLING_DOCUMENT_MODE", "tax_invoice").status_code == 200
    r = _put(client, "EINVOICER_DRIVER", "null")
    assert r.status_code == 400 and r.json()["missing_key"] == "EINVOICER_DRIVER"


def test_clearing_the_seller_vat_under_tax_mode_is_refused(client):
    _put(client, "INVOICE_SELLER_VAT", "300000000000003")
    assert _put(client, "BILLING_DOCUMENT_MODE", "tax_invoice").status_code == 200
    r = client.delete("/api/v1/billing/operator/config/INVOICE_SELLER_VAT", headers=_cfg_admin())
    assert r.status_code == 400
    assert r.json()["error"] == "TAX_PROFILE_INCOMPLETE" and r.json()["missing_key"] == "INVOICE_SELLER_VAT"
