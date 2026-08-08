#!/usr/bin/env python
"""Generate services/<svc>-service/tests/integration/test_backup_api.py from a spec.

spec = {"service": "stream", "extra_env": {"PII_SALT": "..."},
        "seed": [["sources","s1",{"id":"s1","tenant":"acme"}], ...],
        "check": ["sources","s1","tenant","acme"]}   # collection, key, field, expected
"""
from __future__ import annotations

import json
import pathlib
import sys

TEMPLATE = '''"""The backup endpoints, driven over HTTP the way backup-service will drive them.

Proves the wiring — routes exist, are internal-token gated, refuse without a backup key, and complete
a real export→import→verify roundtrip. Rows are seeded straight into the store: this exercises the
backup seam, not the domain write paths.
"""
from __future__ import annotations

import asyncio
import os

import pytest
from fastapi.testclient import TestClient

ITOKEN = "t" * 40
KEK = "backup-key-for-tests"


def _make_app(backup_kek: str = KEK) -> TestClient:
    from app.config import get_settings
    os.environ.update(ENV="test", INTERNAL_SERVICE_TOKEN=ITOKEN, RATELIMIT_MAX="1000",
                      BACKUP_KEK=backup_kek, BODY_LIMIT_BYTES="5000000"{extra_env})
    get_settings.cache_clear()
    from app.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


def _seed(c):
    st = c.app.state.container.datastore
    async def seed():
{seed_lines}
    asyncio.run(seed())


@pytest.fixture
def app_a():
    from app.config import get_settings
    saved = dict(os.environ)
    c = _make_app()
    _seed(c)
    yield c
    os.environ.clear(); os.environ.update(saved); get_settings.cache_clear()


def _h():
    return {{"X-Internal-Token": ITOKEN}}


def test_export_requires_the_internal_token(app_a):
    assert app_a.post("/internal/backup/export", json={{}}).status_code == 401


def test_without_a_backup_key_export_refuses():
    c = _make_app(backup_kek="")
    from app.config import get_settings
    saved = dict(os.environ)
    try:
        assert c.post("/internal/backup/export", headers=_h(), json={{}}).status_code == 503
    finally:
        os.environ.clear(); os.environ.update(saved); get_settings.cache_clear()


def test_export_then_import_restores_into_a_fresh_store(app_a):
    archive = app_a.post("/internal/backup/export", headers=_h(), json={{}}).json()
{count_asserts}

    fresh = _make_app()
    try:
        imp = fresh.post("/internal/backup/import", headers=_h(), json={{"archive": archive}})
        assert imp.status_code == 200, imp.text
        st = fresh.app.state.container.datastore
        loop = asyncio.new_event_loop()
        assert loop.run_until_complete(st.get("{ck_coll}", "{ck_key}"))["{ck_field}"] == {ck_val!r}
        loop.close()
    finally:
        fresh.close()


def test_verify_confirms_the_archive_restores(app_a):
    archive = app_a.post("/internal/backup/export", headers=_h(), json={{}}).json()
    body = app_a.post("/internal/backup/verify", headers=_h(), json={{"archive": archive}}).json()
    assert body["verified"] is True
    assert body["checked"] == body["expected"] >= {min_rows}


def test_a_corrupt_archive_is_refused(app_a):
    archive = app_a.post("/internal/backup/export", headers=_h(), json={{}}).json()
    archive["checksum"] = "sha256:deadbeef"
    assert app_a.post("/internal/backup/import", headers=_h(),
                      json={{"archive": archive}}).status_code == 400
'''


def gen(spec: dict) -> None:
    svc = spec["service"]
    extra_env = ""
    for k, v in (spec.get("extra_env") or {}).items():
        extra_env += f', {k}="{v}"'
    seed_lines = "\n".join(
        f'        await st.put({c!r}, {k!r}, {json.dumps(row)})' for c, k, row in spec["seed"])
    colls = sorted({c for c, _, _ in spec["seed"]})
    count_asserts = "\n".join(f'    assert archive["row_counts"][{c!r}] >= 1' for c in colls)
    ck_coll, ck_key, ck_field, ck_val = spec["check"]
    out = TEMPLATE.format(
        extra_env=extra_env, seed_lines=seed_lines, count_asserts=count_asserts,
        ck_coll=ck_coll, ck_key=ck_key, ck_field=ck_field, ck_val=ck_val,
        min_rows=len(spec["seed"]))
    p = (pathlib.Path(__file__).resolve().parent / "services" / f"{svc}-service" /
         "tests" / "integration" / "test_backup_api.py")
    p.write_text(out, encoding="utf-8")
    print(f"{svc}: wrote {p.relative_to(pathlib.Path(__file__).resolve().parent)}")


if __name__ == "__main__":
    gen(json.loads(sys.argv[1]))
