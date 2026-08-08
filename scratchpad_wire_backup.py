#!/usr/bin/env python
"""Apply the identical, mechanical backup wiring to a service: config block, internal.py endpoints,
manifest (_backup + 3 endpoints + backup key). backup_manifest.py, any reindex, EXEMPT removal, and
the test are done by hand per service (they are the service-specific parts). Idempotent-ish: refuses
if the markers are already present."""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent

CONFIG_BLOCK = '''
    # ── Backup (backup-service orchestrates; this service produces sealed archives) ──────────
    # Seals backup archives at rest. SEPARATE from any live-secrets key by design: restoring must
    # not require the key a running service uses for its own live secrets. Empty means this service
    # cannot produce a backup, and its export endpoint refuses rather than writing one in the clear.
    # SHARED: it must be the SAME value backup-service holds (in the class of PII_SALT /
    # INTERNAL_SERVICE_TOKEN); gen-secrets.py mints it consistently. Two different values and the
    # archives backup-service collects from this service cannot be opened.
    BACKUP_KEK: str = ""
    # The most rows a single collection may hold before a backup FAILS rather than reads on
    # unbounded — a real ceiling an operator sets to the size the data actually reaches.
    BACKUP_MAX_ROWS_PER_COLLECTION: int = 5_000_000
'''

INTERNAL_BLOCK = '''

# ── Backup (backup-service orchestrates; this service produces sealed archives) ──────────────
# What this service holds is declared in app/backup_manifest.py; how it is read lives in
# core/backup.py. backup-service never opens this database — it calls export/verify/import over the
# internal token and stores the sealed archive, never seeing the plaintext.
def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sealer(settings: Settings):
    """The backup (seal, unseal) pair, or a 503 when no backup key is configured.

    Refused rather than defaulted: sealing with an empty key would write a plaintext-equivalent
    archive while every check reported it encrypted — the one outcome a backup must never have.
    """
    if not settings.BACKUP_KEK:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "BACKUP_KEK not configured — set this SHARED key (the SAME value backup-service holds) "
            "on this service, or its archives cannot be opened")
    return _backup.make_sealer(settings.BACKUP_KEK)


class BackupExportIn(BaseModel):
    tenant: str = ""


class BackupVerifyIn(BaseModel):
    archive: dict


class BackupImportIn(BaseModel):
    archive: dict
    overwrite: bool = False
    into_tenant: str = ""


@router.post("/backup/export", dependencies=[Depends(require_internal_token)])
async def backup_export(request: Request, body: BackupExportIn,
                        settings: Settings = Depends(get_settings)) -> dict:
    """Produce a sealed, checksummed archive of this service's declared data."""
    seal, _ = _sealer(settings)
    return await _backup.export(
        request.app.state.container.datastore, BACKUP,
        service=settings.SERVICE_NAME, version=settings.SERVICE_VERSION,
        taken_at=_now_iso(), seal=seal, tenant=body.tenant,
        scan_cap=settings.BACKUP_MAX_ROWS_PER_COLLECTION)


@router.post("/backup/verify", dependencies=[Depends(require_internal_token)])
async def backup_verify(request: Request, body: BackupVerifyIn,
                        settings: Settings = Depends(get_settings)) -> dict:
    """Prove an archive restores, by test-restoring it into a throwaway namespace and comparing."""
    import secrets
    _, unseal = _sealer(settings)
    try:
        return await _backup.verify_restore(
            request.app.state.container.datastore, body.archive,
            unseal=unseal, token=secrets.token_hex(8), declaration=BACKUP)
    except _backup.BackupError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.post("/backup/import", dependencies=[Depends(require_internal_token)])
async def backup_import(request: Request, body: BackupImportIn,
                        settings: Settings = Depends(get_settings)) -> dict:
    """Restore an archive this service produced. Idempotent; leaves newer rows alone by default."""
    _, unseal = _sealer(settings)
    try:
        return await _backup.import_archive(
            request.app.state.container.datastore, body.archive,
            unseal=unseal, overwrite=body.overwrite, into_tenant=body.into_tenant,
            declaration=BACKUP)
    except _backup.BackupError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
'''

MANIFEST_BACKUP_FN = '''def _backup() -> dict:
    """This service's backup declaration (app/backup_manifest.py), or an empty one.

    Published in the manifest so the backup fan-out discovers what to capture the same way the DSAR
    fan-out discovers who holds a person — a holder nobody declares is one nobody backs up."""
    try:
        from app.backup_manifest import BACKUP
    except Exception:
        return {"object_prefix": "", "collections": [], "excluded": []}
    return BACKUP


def build_manifest('''

MANIFEST_ENDPOINTS = '''            # Backup fan-out (backup-service orchestrates): export + verify + import this
            # service's data. Declared so the fan-out discovers this holder — an undeclared holder
            # is one nobody backs up.
            {"path": "/internal/backup/export", "tier": "internal", "method": "POST"},
            {"path": "/internal/backup/import", "tier": "internal", "method": "POST"},
            {"path": "/internal/backup/verify", "tier": "internal", "method": "POST"},
'''


def wire(service: str, *, config_anchor: str, internal_import_anchor: str,
         manifest_endpoint_anchor: str):
    base = ROOT / "services" / f"{service}-service" / "app"
    # 1) config
    cfg = base / "config.py"
    t = cfg.read_text(encoding="utf-8")
    assert "BACKUP_KEK" not in t, f"{service}: config already has BACKUP_KEK"
    assert t.count(config_anchor) == 1, f"{service}: config anchor x{t.count(config_anchor)}"
    t = t.replace(config_anchor, config_anchor + CONFIG_BLOCK, 1)
    cfg.write_text(t, encoding="utf-8")

    # 2) internal.py — imports + endpoints
    intl = base / "api" / "internal.py"
    t = intl.read_text(encoding="utf-8")
    assert "backup/export" not in t, f"{service}: internal already wired"
    assert t.count(internal_import_anchor) == 1, f"{service}: internal import anchor"
    imports = ("from app.backup_manifest import BACKUP\n"
               "from app.core import backup as _backup\n")
    t = t.replace(internal_import_anchor, imports + internal_import_anchor, 1)
    t = t.rstrip("\n") + "\n" + INTERNAL_BLOCK
    intl.write_text(t, encoding="utf-8")

    # 3) manifest.py — _backup fn, endpoints, backup key
    man = base / "manifest.py"
    t = man.read_text(encoding="utf-8")
    assert '"backup":' not in t, f"{service}: manifest already wired"
    assert t.count("def build_manifest(") == 1
    t = t.replace("def build_manifest(", MANIFEST_BACKUP_FN, 1)
    assert t.count(manifest_endpoint_anchor) == 1, f"{service}: manifest endpoint anchor"
    t = t.replace(manifest_endpoint_anchor, manifest_endpoint_anchor + MANIFEST_ENDPOINTS, 1)
    assert t.count('"admin_actions": _admin_actions(),') == 1
    t = t.replace('"admin_actions": _admin_actions(),',
                  '"admin_actions": _admin_actions(),\n        "backup": _backup(),   # what backup-service must capture to restore this service',
                  1)
    man.write_text(t, encoding="utf-8")
    print(f"{service}: wired config + internal + manifest")


if __name__ == "__main__":
    import json
    spec = json.loads(sys.argv[1])
    wire(spec["service"], config_anchor=spec["config_anchor"],
         internal_import_anchor=spec["internal_import_anchor"],
         manifest_endpoint_anchor=spec["manifest_endpoint_anchor"])
