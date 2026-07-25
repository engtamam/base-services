"""Export and import this service's data, for backup-service to orchestrate.

This is the plug-and-play seam. backup-service never opens this service's database — it calls
`export` to get a sealed archive it stores, and `import_archive` to put one back. What a collection
means, how it is read, and how its rows are keyed is knowledge this service has and the orchestrator
does not, so it lives here and is exposed through a small stable contract.

THE ARCHIVE IS SELF-VERIFYING. The payload is sealed under a backup key, and a checksum over the
sealed bytes is written LAST. A backup half-written when a process died is caught two ways: the JSON
does not parse, or the checksum does not cover the truncated body. Either way it is refused on
import rather than restored as garbage — a corrupt backup silently restored is worse than an obvious
failure, because it destroys the good data it lands on.

IMPORT IS IDEMPOTENT AND DOES NOT CLOBBER. Restore is keyed on each row's own identity, so running
it twice changes nothing the second time. A row that already exists is LEFT ALONE unless the caller
asks to overwrite — a restore into a tenant that partly exists must not bulldoze rows that were
written after the backup was taken. That is what makes a half-finished restore safe to re-run.

WHOLE-COLLECTION READS ARE DELIBERATE HERE. `scan_all` is banned on a request path because it costs
the size of the data; a backup is that operation by definition. A per-tenant export uses the
tenant-scoped `find` instead, so backing up one customer does not read every customer's rows.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

ARCHIVE_FORMAT = "base-backup/v1"

# How many rows a single find() page pulls when walking a tenant-scoped collection. A page, not the
# whole thing, so a large tenant is streamed rather than loaded whole into one request's memory.
_PAGE = 500


class BackupError(ValueError):
    """An archive that cannot be trusted — wrong format, failed checksum, or undecryptable."""


def _checksum(sealed: str) -> str:
    return "sha256:" + hashlib.sha256(sealed.encode("utf-8")).hexdigest()


async def _read_collection(datastore, name: str, tenant_scoped: bool, tenant: str,
                           scan_cap: int) -> list[dict[str, Any]]:
    """Every row of one collection — one tenant's if scoped and a tenant is given, else all."""
    if tenant and tenant_scoped:
        # find() requires a non-empty `where`, which is exactly right: a per-tenant backup must
        # never accidentally read across tenants. Paged, so a big customer streams.
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            rows, cursor = await datastore.find(name, {"tenant": tenant}, limit=_PAGE,
                                                after=cursor, sort=("id", "asc"))
            out.extend(rows)
            if not cursor:
                return out
    # sweeper: a whole-collection read IS the backup operation, not a shortcut around an index.
    # Bounded by scan_cap so a runaway collection fails the backup rather than exhausting memory.
    return await datastore.scan_all(name, scan_cap)


async def export(datastore, declaration: dict[str, Any], *, service: str, version: str,
                 taken_at: str, seal: Callable[[str], str], tenant: str = "",
                 scan_cap: int) -> dict[str, Any]:
    """Produce a sealed, checksummed archive of this service's declared data.

    `seal` turns a string into its sealed form under the backup key; the payload is never written
    in the clear. `tenant` empty means the whole service; a value means just that customer's
    tenant-scoped rows (global collections are always included whole — a shared plan catalogue is
    not one tenant's to omit).
    """
    payload: dict[str, Any] = {}
    for entry in declaration.get("collections", []):
        name = entry.get("name")
        if not name:
            continue
        payload[name] = await _read_collection(
            datastore, name, bool(entry.get("tenant_scoped")), tenant, scan_cap)

    sealed = seal(json.dumps({"service": service, "version": version, "taken_at": taken_at,
                              "tenant": tenant, "collections": payload},
                             separators=(",", ":"), sort_keys=True))
    return {
        "format": ARCHIVE_FORMAT,
        "service": service,
        "taken_at": taken_at,
        "tenant": tenant,
        # Counts in the CLEAR so an operator can see what an archive holds without unsealing it —
        # useful, and no more disclosure than the manifest already makes.
        "row_counts": {name: len(rows) for name, rows in payload.items()},
        "sealed": sealed,
        # LAST. A truncated write loses this or the JSON's closing brace, and either way the
        # archive is refused rather than half-restored.
        "checksum": _checksum(sealed),
    }


def verify(archive: dict[str, Any]) -> None:
    """Refuse an archive that is not what it claims, BEFORE anything is decrypted or written."""
    if archive.get("format") != ARCHIVE_FORMAT:
        raise BackupError(f"not a {ARCHIVE_FORMAT} archive")
    sealed, checksum = archive.get("sealed"), archive.get("checksum")
    if not sealed or not checksum:
        raise BackupError("archive is missing its sealed body or checksum — truncated write?")
    if _checksum(sealed) != checksum:
        raise BackupError("archive checksum does not match its body — corrupt or truncated")


async def import_archive(datastore, archive: dict[str, Any], *,
                         unseal: Callable[[str], str], overwrite: bool = False) -> dict[str, Any]:
    """Restore an archive, idempotently and without clobbering newer rows.

    `unseal` reverses `seal`; if the backup key does not match the one the archive was sealed with,
    it raises, and this surfaces that as a clear refusal rather than writing garbage.
    """
    verify(archive)
    try:
        body = json.loads(unseal(archive["sealed"]))
    except BackupError:
        raise
    except Exception as exc:  # noqa: BLE001 — a wrong backup key, or a tampered blob
        raise BackupError(
            "archive could not be decrypted — the backup key does not match the one it was sealed "
            "with. Restore with the key the backup was taken under.") from exc

    inserted = skipped = overwritten = 0
    for name, rows in (body.get("collections") or {}).items():
        for row in rows:
            key = row.get("id")
            if not key:
                # A row with no identity cannot be restored idempotently — restoring it twice would
                # duplicate it. Refuse the archive rather than quietly double-write.
                raise BackupError(f"{name}: a row has no id; cannot restore without a row identity")
            existing = await datastore.get(name, key)
            if existing is None:
                await datastore.put(name, key, row)
                inserted += 1
            elif overwrite:
                await datastore.put(name, key, row)
                overwritten += 1
            else:
                # Left alone. A restore into a partly-existing tenant must not bulldoze a row that
                # was written after the backup, and re-running a half-finished restore must be a
                # no-op on what already landed.
                skipped += 1
    return {"service": body.get("service"), "tenant": body.get("tenant", ""),
            "inserted": inserted, "overwritten": overwritten, "skipped": skipped}

_SEAL_PREFIX = "v1:gcm:"


def make_sealer(kek: str):
    """(seal, unseal) closures over a backup key that is SEPARATE from any service's SECRETS_KEK.

    Separate on purpose: a backup key protects data at rest in the archive store, and restoring
    must not require handing out the key every running service uses for its own live secrets. A
    stolen archive store yields ciphertext; a stolen archive store plus the backup key yields the
    data — which is the same exposure the live database already has, so this adds a layer rather
    than trading one away.

    The key is stretched with scrypt (as the credential store does), so a weak `BACKUP_KEK` is not
    used raw. Raises if no key is configured, because sealing with an empty key writes
    plaintext-equivalent while every check reports 'encrypted'.
    """
    import base64
    import hashlib
    import os

    if not kek:
        raise BackupError("BACKUP_KEK is required to seal a backup — it must never be empty")
    key = hashlib.scrypt(kek.encode("utf-8"), salt=b"base-backup-v1", n=2**14, r=8, p=1, dklen=32)

    def seal(text: str) -> str:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        iv = os.urandom(12)
        return _SEAL_PREFIX + base64.b64encode(
            iv + AESGCM(key).encrypt(iv, text.encode("utf-8"), None)).decode("ascii")

    def unseal(sealed: str) -> str:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        if not sealed.startswith(_SEAL_PREFIX):
            raise BackupError("sealed body is not in a recognised format")
        blob = base64.b64decode(sealed[len(_SEAL_PREFIX):])
        return AESGCM(key).decrypt(blob[:12], blob[12:], None).decode("utf-8")

    return seal, unseal
