"""GET /internal/verification/identity-grant-token — onboarding's once-per-verified-identity seam.

The token must be: purpose-scoped (HMAC over the claim_ref, NEVER the claim_ref itself), stable
(same identity → same token), and withheld unless a VERIFIED record for that exact identity claim
exists (so a grant gated on it fails CLOSED). onboarding-r2-03.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import os

from app.core.models import REFUSED, VERIFIED, Verification
from app.core.stores import VerificationStore
from tests.conftest import INTERNAL_TOKEN

_H = {"X-Internal-Token": INTERNAL_TOKEN}


def _seed(client, **kw) -> Verification:
    store = VerificationStore(client.app.state.container.datastore)
    rec = Verification(id=kw.pop("id"), **kw)
    asyncio.run(store.put(rec))
    return rec


def test_requires_internal_token(client):
    r = client.get("/internal/verification/identity-grant-token",
                   params={"tenant": "t1", "subject": "s1", "claim_type": "commercial_registration"})
    assert r.status_code == 401


def test_verified_identity_yields_purpose_scoped_token(client):
    _seed(client, id="v-grant-1", tenant="t1", subject="s-grant-1",
          claim_type="commercial_registration", claim_ref="CLAIMREF-ABC", verdict=VERIFIED)
    r = client.get("/internal/verification/identity-grant-token", headers=_H,
                   params={"tenant": "t1", "subject": "s-grant-1",
                           "claim_type": "commercial_registration"})
    assert r.status_code == 200
    token = r.json()["token"]

    salt = os.environ.get("PII_SALT", "")
    expected = hmac.new(salt.encode(), b"onboarding-grant|CLAIMREF-ABC", hashlib.sha256).hexdigest()
    assert token == expected
    # NEVER the claim_ref itself.
    assert token != "CLAIMREF-ABC" and "CLAIMREF-ABC" not in token


def test_token_is_stable_for_same_identity(client):
    _seed(client, id="v-grant-2", tenant="t2", subject="s-grant-2",
          claim_type="commercial_registration", claim_ref="CLAIMREF-XYZ", verdict=VERIFIED)
    p = {"tenant": "t2", "subject": "s-grant-2", "claim_type": "commercial_registration"}
    a = client.get("/internal/verification/identity-grant-token", headers=_H, params=p).json()["token"]
    b = client.get("/internal/verification/identity-grant-token", headers=_H, params=p).json()["token"]
    assert a and a == b


def test_unverified_verdict_withholds_token(client):
    _seed(client, id="v-grant-3", tenant="t3", subject="s-grant-3",
          claim_type="commercial_registration", claim_ref="CLAIMREF-REFUSED", verdict=REFUSED)
    r = client.get("/internal/verification/identity-grant-token", headers=_H,
                   params={"tenant": "t3", "subject": "s-grant-3",
                           "claim_type": "commercial_registration"})
    assert r.status_code == 200 and r.json()["token"] == ""


def test_no_record_withholds_token(client):
    r = client.get("/internal/verification/identity-grant-token", headers=_H,
                   params={"tenant": "t4", "subject": "nobody", "claim_type": "commercial_registration"})
    assert r.status_code == 200 and r.json()["token"] == ""
