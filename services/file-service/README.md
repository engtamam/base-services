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
| user | `POST /api/v1/files` (raw body + `X-Filename`, `Content-Type`) | `files.upload` | upload (small, inline) |
| user | `GET /api/v1/files/{id}` | `files.read` | download (tenant-scoped) |
| user | `GET /api/v1/files/{id}/metadata` | `files.read` | metadata |
| user | `POST /api/v1/files/{id}/signed-url` | `files.read` | mint a signed URL |
| user | `DELETE /api/v1/files/{id}` | `files.upload` | delete (ref-count aware) |
| user | `POST /api/v1/files/uploads` | `files.upload` | **initiate a large, direct-to-bucket upload** |
| user | `POST /api/v1/files/uploads/{id}/complete` | `files.upload` | finish a large upload |
| user | `GET /api/v1/files/uploads/{id}` | `files.read` | large-upload status |
| user | `DELETE /api/v1/files/uploads/{id}` | `files.upload` | cancel a large upload (releases the multipart) |
| public | `GET /files/signed/{token}` | — | download via signed URL (no auth; token is the grant) |

Small upload is a **raw body** (the file bytes) with `X-Filename` + `Content-Type` headers —
no `python-multipart` needed. Max size = `MAX_UPLOAD_BYTES` (≤ `BODY_LIMIT_BYTES`).

## Two models: small-in-app and large-direct-to-bucket (Phase 34)

Small files (≤ `MAX_INLINE_BYTES`) go **through the app**: scanned and hashed in-request, then stored
— today's path. A multi-GB object cannot: you can't hold it in memory or scan it in the request. So
above the boundary the client uploads **straight to the bucket** and the app never touches the bytes.
This path is available only when the wired object store can presign (the `s3` driver); a client
discovers it from the manifest's `features.large_uploads`. Over the boundary on a non-presign store is
a **413**, never a silent buffer.

A file is **never readable until scanned**, whichever path it took. A large upload lands on a
per-session **staging** key, is hashed + scanned asynchronously, and is promoted to its
content-addressed key only when the hash matches and the scan is clean — otherwise it is
**quarantined** and the staging object is discarded (a lying client can never overwrite a shared
content blob). Dedup is possession-safe: an owner who already holds a hash skips re-uploading; a
different owner must upload and verify.

```bash
# 1. initiate — declare the file's sha256 + size; get a presigned multipart plan (or a dedup hit)
curl -XPOST $GW/api/v1/files/uploads -H "Authorization: Bearer $JWT" \
  -d '{"filename":"movie.mp4","content_type":"video/mp4","sha256":"<hex>","size":5368709120}'
#   → {"deduplicated":false,"session":{"id":"...","status":"pending"},
#      "upload":{"part_size":8388608,"part_count":640,"parts":[{"part_number":1,"url":"https://..."}]}}

# 2. PUT each part DIRECTLY to its presigned URL (bytes never touch the app); keep each ETag
curl -XPUT "<parts[0].url>" --data-binary @part-1  # -> response ETag header

# 3. complete — hand back the part ETags; the object is assembled and queued for scanning
curl -XPOST $GW/api/v1/files/uploads/$SID/complete -H "Authorization: Bearer $JWT" \
  -d '{"parts":[{"part_number":1,"etag":"..."}]}'          # → {"status":"scanning"}

# 4. poll status until ready (a CronJob drains the scan queue)
curl $GW/api/v1/files/uploads/$SID -H "Authorization: Bearer $JWT"   # → {"status":"ready","file_id":"..."}

# 5. download — on an s3 store this 302-redirects straight to a short-lived presigned bucket GET
curl -L $GW/api/v1/files/$FID -H "Authorization: Bearer $JWT"
```

Verified end-to-end against a real MinIO: initiate → parts PUT direct-to-bucket → complete → scan →
ready → download redirect → bytes fetched from the bucket, sha matches.

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

Thumbnails / preview (Pillow → optional `ThumbnailGenerator`) · tus resumable protocol · CDN /
image transforms · a non-S3 presign adapter (GCS/Azure). Large direct-to-bucket multipart uploads and
S3 presigned URLs **shipped in Phase 34**.

## Rate limiting (every request, config-driven)

Sliding-window (GCRA) limiter — **no boundary burst**, and **atomic** (a check-and-increment race
cannot exceed the limit). Every response carries `RateLimit-Limit/Remaining/Reset`; a 429 also
carries `Retry-After`. On a Redis outage it **degrades to a per-pod limiter** (coarser, still
limiting) rather than failing open or 500-ing.

| Choice | Env knob | Options / example |
|--------|----------|-------------------|
| Backend | `RATELIMITER_DRIVER` | `memory`(per-pod) · `redis`(shared) |
| Budget | `RATELIMIT_MAX` / `RATELIMIT_WINDOW_SECONDS` | e.g. `100` / `60` |
| Key | `RATELIMIT_KEY_PARTS` | `subject,ip` · add `tenant`/`route` |
| Per-route budgets | `RATELIMIT_ROUTES` | `[{"prefix","class","limit","window_s","cost"}]` |
| Only count failures | `RATELIMIT_COUNT_STATUSES` | e.g. `401` (a 403 will not lock a user out) |
| On Redis outage | `RATELIMIT_DEGRADE` | `local`(default) · `closed` · `open` |

> The subject is resolved **inside** the limiter (its own optional dependency), not read from
> pre-set request state — so an authenticated request keys per-subject regardless of the route's
> dependency order. No "one office NAT shares a bucket" surprise. Enforced by `safety:edge`.

Operator unblock (never `FLUSHALL`): `POST /internal/ratelimit/unblock {"key":"<key>"}`.

> The public edge (Caddy) is generated from `deploy/edge/edge.json` by `tools/edge/render.py` —
> one config source, injection-refused, HSTS only under TLS, non-root container. `nginx` is a
> future renderer of the same shape.
