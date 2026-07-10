# file-service

> Self-contained, plug-and-play **FastAPI** file service (SaaS checklist §2). Upload /
> download / delete with **size + type + magic-byte validation** and malware
> **scanning**, **content-addressed storage** (sha256 dedup) + **ref-counting** (safe
> delete), and **signed URLs** (expiring, tamper-proof). Reuses the `ObjectStore`
> port; adds pluggable `Scanner` + `SignedUrlProvider`. Backend only. Built from
> `_template`. **Zero new deps** (raw-body upload — no multipart).

---

## API

| Tier | Method · Path | Cap | Purpose |
|------|---------------|-----|---------|
| user | `POST /api/v1/files` (raw body + `X-Filename`, `Content-Type`) | `files.upload` | upload |
| user | `GET /api/v1/files/{id}` | `files.read` | download (tenant-scoped) |
| user | `GET /api/v1/files/{id}/metadata` | `files.read` | metadata |
| user | `POST /api/v1/files/{id}/signed-url` | `files.read` | mint a signed URL |
| user | `DELETE /api/v1/files/{id}` | `files.upload` | delete (ref-count aware) |
| public | `GET /files/signed/{token}` | — | download via signed URL (no auth; token is the grant) |

Upload is a **raw body** (the file bytes) with `X-Filename` + `Content-Type` headers —
no `python-multipart` needed. Max size = `MAX_UPLOAD_BYTES` (≤ `BODY_LIMIT_BYTES`).

## Content-addressing + ref-counting

The blob key is `sha256(bytes)`, so identical uploads **dedup** to a single stored
blob. A **ref-count** tracks how many file records share a blob; `DELETE` removes the
metadata and decrements the count — the blob itself is deleted only when the **last**
reference goes away. Verified live: delete #1 keeps the blob, delete #2 removes it.

## Signed URLs

`POST /{id}/signed-url` returns `/files/signed/<token>` where the token is a
stateless, HMAC-signed `{key, exp}` — tamper-proof and expiring. The public endpoint
verifies the signature + expiry and serves the bytes; **tampered or expired → 403**.
Works with **any** ObjectStore (no S3 presign dependency).

## Plug-and-play (config only)

| Choice | Knob | Options |
|--------|------|---------|
| Storage | `OBJECTSTORE_DRIVER` | local · memory · s3 |
| Malware scan | `SCANNER_DRIVER` | none · **magic** · clamav |
| Signed URLs | `SIGNED_URL_DRIVER` | hmac (s3-presign: future) |
| Upload cap | `MAX_UPLOAD_BYTES` | int |
| Allowed types | `ALLOWED_CONTENT_TYPES` | comma list (empty = any) |
| Signed TTL | `SIGNED_URL_TTL_SECONDS` | int |

## Security

Defends unrestricted file upload (CWE-434): **size** cap, **content-type allowlist**,
**magic-byte** check (blocks executables + type mismatches), optional **ClamAV** scan.
Path-safe object keys (sha256), `Content-Disposition` filename **sanitized** (no header
injection), signed-URL tamper/expiry rejected, deny-by-default, **cross-tenant hidden**
(404, not 403).

## Run

```bash
py -3.12 -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev,ci]"
uvicorn app.main:app
pytest -q ; python ci/security_gate.py
```

## Out of scope (later)

Thumbnails / preview (Pillow → optional `ThumbnailGenerator`) · chunked / resumable
uploads · S3 presigned URLs · CDN / image transforms.
