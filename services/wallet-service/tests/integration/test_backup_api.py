"""The backup endpoints, driven over HTTP the way backup-service will drive them.

This is money, so the roundtrip is proven the way the service itself proves correctness: restore into
a FRESH store and reconcile() — the restored wallet must still equal the sum of its restored ledger,
and its sequence counter must survive so the next spend does not re-issue a ledger number. The ledger
is keyed by a composite ("{tenant}:{seq:012d}"), so this also exercises the backup core's key
template — a ledger restored under its led_ id would silently drop out of the seq-ordered history.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

ITOKEN = "t" * 40   # MUST match tests/conftest's INTERNAL_SERVICE_TOKEN so the session app is undisturbed
KEK = "backup-key-for-tests"


def _make_app(backup_kek: str = KEK) -> TestClient:
    from app.config import get_settings
    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN=ITOKEN, RATELIMIT_MAX="1000",
                      BACKUP_KEK=backup_kek)
    get_settings.cache_clear()
    from app.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture
def app_a():
    from app.config import get_settings
    saved = dict(os.environ)
    c = _make_app()
    # Real money movements: two credits and a spend -> a wallet, three ledger entries, seq=3.
    # `cash` is a configured taxed fund (WALLET_FUNDS default); spend takes the PRE-VAT `net`.
    assert c.post("/internal/wallet/credit", headers=_h(),
                  json={"tenant": "acme", "fund": "cash", "amount": 1000}).status_code == 200
    assert c.post("/internal/wallet/credit", headers=_h(),
                  json={"tenant": "acme", "fund": "cash", "amount": 500}).status_code == 200
    assert c.post("/internal/wallet/spend", headers=_h(),
                  json={"tenant": "acme", "net": 300}).status_code == 200
    yield c
    os.environ.clear()
    os.environ.update(saved)
    get_settings.cache_clear()


def _h():
    return {"X-Internal-Token": ITOKEN}


# ── authorisation ─────────────────────────────────────────────────────────────
def test_export_requires_the_internal_token(app_a):
    assert app_a.post("/internal/backup/export", json={}).status_code == 401


# ── no key ────────────────────────────────────────────────────────────────────
def test_without_a_backup_key_export_refuses():
    c = _make_app(backup_kek="")
    from app.config import get_settings
    saved = dict(os.environ)
    try:
        assert c.post("/internal/backup/export", headers=_h(), json={}).status_code == 503
    finally:
        os.environ.clear(); os.environ.update(saved); get_settings.cache_clear()


# ── roundtrip into a FRESH store, proven by reconcile ───────────────────────────
def test_restore_into_a_fresh_store_reconciles(app_a):
    exp = app_a.post("/internal/backup/export", headers=_h(), json={})
    assert exp.status_code == 200, exp.text
    archive = exp.json()
    assert archive["row_counts"]["wallets"] >= 1
    assert archive["row_counts"]["ledger"] >= 3        # two credits + a spend

    before = app_a.get("/internal/wallet/acme", headers=_h()).json()

    # A brand-new service with an empty store — the disaster-recovery case.
    fresh = _make_app()
    try:
        assert fresh.get("/internal/wallet/acme", headers=_h()).json().get("balance", 0) == 0
        imp = fresh.post("/internal/backup/import", headers=_h(), json={"archive": archive})
        assert imp.status_code == 200, imp.text

        # The balance came back...
        after = fresh.get("/internal/wallet/acme", headers=_h()).json()
        assert after["balance"] == before["balance"]

        # ...and the wallet still equals the sum of its ledger — the money record is CONSISTENT,
        # and the seq counter survived (reconcile walks seq 1..N and finds every entry).
        rec = fresh.get("/internal/wallet/acme/reconcile", headers=_h()).json()
        assert rec["consistent"] is True
        assert rec["entries"] >= 3
    finally:
        fresh.close()


def test_ledger_restores_under_its_composite_key_not_its_row_id(app_a):
    archive = app_a.post("/internal/backup/export", headers=_h(), json={}).json()
    fresh = _make_app()
    try:
        fresh.post("/internal/backup/import", headers=_h(), json={"archive": archive})
        import asyncio
        st = fresh.app.state.container.datastore
        loop = asyncio.new_event_loop()
        # The first ledger entry is readable under the SAME key the service writes (_ledger_key).
        row = loop.run_until_complete(st.get("ledger", "acme:000000000001"))
        assert row is not None and row["seq"] == 1
        loop.close()
    finally:
        fresh.close()


def test_verify_confirms_the_archive_restores(app_a):
    archive = app_a.post("/internal/backup/export", headers=_h(), json={}).json()
    r = app_a.post("/internal/backup/verify", headers=_h(), json={"archive": archive})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verified"] is True
    assert body["checked"] == body["expected"] >= 4    # 1 wallet + >=3 ledger


def test_a_corrupt_archive_is_refused(app_a):
    archive = app_a.post("/internal/backup/export", headers=_h(), json={}).json()
    archive["checksum"] = "sha256:deadbeef"
    r = app_a.post("/internal/backup/import", headers=_h(), json={"archive": archive})
    assert r.status_code == 400
