# Phase 11 — file-service (detailed steps)

> **Goal:** the Tier-1 **file-service** (SaaS checklist §2). Upload / download /
> delete files with **size + type + magic-byte validation**, malware **scanning**,
> **content-addressed storage** (sha256 dedup) with **ref-counting** (safe delete),
> and **signed URLs** (expiring, tamper-proof) for secure access. Reuses the
> existing **`ObjectStore`** port; adds pluggable **`Scanner`** + **`SignedUrlProvider`**.
> Plug-and-play, config-driven, **zero new deps** (raw-body upload — no multipart).
>
> _Reference: `docs/architecture-plan.md` §5 (Tier-1) · `docs/saas-checklist.md` §2._
> _Memory: self-contained · zero-hardcoded · ports-and-adapters · production-ready · permissive · CWE-guard · KSA · backend-only._

---

## 0. Definition of done (Phase 11)

- [ ] `services/file-service` generated from `_template` (self-contained; inherits ports, auth, CWE guard, CI gate).
- [ ] **Upload** (raw body + `X-Filename` / `Content-Type`) → validate **size** (`MAX_UPLOAD_BYTES`) + **type allowlist** + **magic-byte** match → **scan** → store.
- [ ] **Content-addressed storage** — blob key = `sha256(bytes)` (dedup); a **ref-count** tracks sharers so a delete only removes the blob at 0.
- [ ] **Pluggable by config:** `ObjectStore` (local/s3/memory), `Scanner` (none/magic/clamav), `SignedUrlProvider` (hmac/s3). Metadata via `DataStore`.
- [ ] **Signed URLs** — issue a token (key + expiry, HMAC-signed); a **public** endpoint verifies it (sig + expiry) and serves the bytes; tampered/expired → 403.
- [ ] **Download / metadata / delete** — token-authenticated, tenant-scoped.
- [ ] **Security:** size/type/magic validation (CWE-434 unrestricted upload), path-safe keys (ObjectStore), scan malware, no cross-tenant access, safe errors.
- [ ] **Zero new deps · zero-hardcoded**; `pytest` + `ci/security_gate.py` green; live boot proven (upload → download → dedup → signed-url → delete).

---

## 1. Prerequisites & decisions

1. **Zero new deps.** Uploads use the **raw request body** (`request.body()`) + `X-Filename` / `Content-Type` headers — no `python-multipart`. Hashing is stdlib `hashlib`; signing is stdlib `hmac`. Storage is the `ObjectStore` port; metadata is `DataStore`.
2. **Body limit.** file-service raises the inherited `BODY_LIMIT_BYTES` default (e.g. 25 MiB) so uploads fit; the route also enforces `MAX_UPLOAD_BYTES` (must be ≤ body limit).
3. **Content-addressed + ref-count.** Blob key = `sha256`; a `blob_refs` counter enables dedup + safe delete (from ammar, generalized). Deleting a file decrements; the blob is removed only at 0.
4. **Thumbnails deferred.** Preview/thumbnail generation needs an image lib (Pillow) — a future optional `ThumbnailGenerator` adapter, kept out to preserve zero-new-deps.

---

## 2. Steps

### Step 11.1 — Generate skeleton
- `python scripts/create_service.py file-service --desc "Files: upload, storage, signed URLs"`.
- No new deps → verify reuses the template venv. Bump `BODY_LIMIT_BYTES` default in config for uploads. Confirm inherited tests + gate green.
- **Check:** skeleton verified green.

### Step 11.2 — Config (zero-hardcoded)
- `MAX_UPLOAD_BYTES` (25 MiB), `ALLOWED_CONTENT_TYPES` (comma list; empty = allow any), `SCANNER_DRIVER` (`none`), `SIGNED_URL_DRIVER` (`hmac`), `SIGNED_URL_SECRET`, `SIGNED_URL_TTL_SECONDS` (300), `MAGIC_STRICT` (bool). Bump `BODY_LIMIT_BYTES` default. Update `.env.example`.
- **Check:** boots with defaults; fail-fast on bad values.

### Step 11.3 — Ports
- `app/ports/scanner.py` (`scan(data, content_type) -> None` raises `Infected`), `app/ports/signed_url.py` (`sign(key, extra) -> token`, `verify(token) -> dict|None`).
- **Check:** import pulls no third-party driver.

### Step 11.4 — Adapters + registry
- Scanner: `none.py` (allow), `magic.py` (magic-byte signature check vs declared type + a dangerous-type blocklist), `clamav.py` (lazy clamd — infra-gated). SignedUrl: `hmac.py` (stateless `base64(payload).sig`, stdlib), `s3_presigned.py` (lazy, when ObjectStore=s3).
- Extend `container.py` with `scanner`, `signed_url` (allowlist, fail-fast).
- **Check:** unknown driver → ConfigError; magic scanner blocks a mismatch; hmac sign/verify round-trips; hmac needs a secret.

### Step 11.5 — Domain: model + repository
- `core/models.py` `FileObject` (id, tenant, owner, filename, content_type, size, sha256, storage_key, created, metadata).
- `core/file_repository.py` over `DataStore`: `create`, `get`, `list(tenant)`, `delete`, plus **ref-count** helpers `incr_ref(key)`, `decr_ref(key) -> remaining`.
- **Check:** create/get/list/delete + ref-count increment/decrement.

### Step 11.6 — Core FileService
- `core/file_service.py`: `upload(tenant, owner, filename, content_type, data)` → size check → type allowlist → magic check → **scan** → `sha256` → `ObjectStore.put(sha256, data)` (dedup: put is idempotent) → `incr_ref` → record `FileObject`; `download(id)` → fetch bytes; `metadata(id)`; `delete(id)` → remove record → `decr_ref` → `ObjectStore.delete(sha256)` at 0; `signed_url(id)` / `resolve_signed(token)`.
- **Check:** unit: roundtrip, size/type/magic/scan rejects, dedup (two uploads same bytes → one blob, ref 2), delete decrements + removes at 0, signed url valid/expired/tampered.

### Step 11.7 — Routes
- User (`require_user`): `POST /api/v1/files` (raw body + `X-Filename`; cap `files.upload`), `GET /api/v1/files/{id}` (download; cap `files.read`, tenant-scoped), `GET /api/v1/files/{id}/metadata`, `POST /api/v1/files/{id}/signed-url`, `DELETE /api/v1/files/{id}`.
- Public: `GET /files/signed/{token}` (no auth — the signed URL; verifies token → serves bytes; expired/tampered → 403).
- Wire `rate_limit`, deny-by-default, `guard.step("respond")` on metadata, `guard.step("file_write")` on the object key.
- **Check:** routes mount; deny-by-default; public signed endpoint works only with a valid token.

### Step 11.8 — Manifest & wiring
- Manifest caps (`files.upload`, `files.read`) + endpoints; ports listed (object_store/scanner/signed_url drivers).
- **Check:** `/internal/manifest` lists file ports + endpoints.

### Step 11.9 — Tests
- Unit: size/type/magic/scan rejects, dedup + ref-count, delete removes blob at 0, hmac signed-url valid/expired/tampered, tenant scoping.
- Integration: upload → download (bytes match) → metadata → signed-url → public fetch via token → delete → 404; oversized/blocked-type rejected; cross-tenant download 403/404.
- Security: unrestricted-upload defenses (magic mismatch blocked), signed-url tamper/expiry rejected, path-safe keys, deny-by-default, no cross-tenant leakage.
- **Check:** all green.

### Step 11.10 — Verify + live boot
- `pytest` + `ci/security_gate.py` green.
- Live boot: upload a file → download (bytes identical) → upload same bytes again (dedup, ref 2) → mint signed URL → fetch via public token → delete one (blob stays, ref 1) → delete other (blob gone).
- **Check:** end-to-end works.

### Step 11.11 — Docs
- Service `README.md`: what it is, upload/download API, content-addressing + ref-count, signed-url flow, plug-and-play matrix, security.
- Update `docs/architecture-plan.md` (file ✅) + build-progress memory.
- **Check:** README complete.

---

## 3. Out of scope (deferred)

- **Thumbnails / preview** (Pillow) → optional `ThumbnailGenerator` adapter later.
- **Chunked / resumable uploads** → later.
- **Virus-scan via a real ClamAV cluster** → the clamav adapter is provided; deploy clamd.
- **Image transforms / CDN** → infra / later.

---

## 4. Execution checklist

| Task | Step | Status |
|---|---|---|
| #109 | 11.1 Skeleton | ✅ completed |
| #110 | 11.2 Config | ✅ completed |
| #111 | 11.3 Ports | ✅ completed |
| #112 | 11.4 Adapters + registry | ✅ completed |
| #113 | 11.5 Domain model + repository | ✅ completed |
| #114 | 11.6 Core FileService | ✅ completed |
| #115 | 11.7 Routes | ✅ completed |
| #116 | 11.8 Manifest & wiring | ✅ completed |
| #117 | 11.9 Tests | ✅ completed |
| #118 | 11.10 Verify + live boot | ✅ completed |
| #119 | 11.11 Docs | ✅ completed |
| —   | DoD (§0) all checked | ✅ completed (135 tests; gate PASS; lifecycle live) |

_Legend: ⬜ pending · 🟡 in-progress · ✅ completed._

---

## 5. Why this is plug-and-play + smart

| Choice | Knob | Options |
|---|---|---|
| Storage backend | `OBJECTSTORE_DRIVER` | local · memory · s3 |
| Malware scan | `SCANNER_DRIVER` | none · magic · clamav |
| Signed URLs | `SIGNED_URL_DRIVER` | hmac · s3 |
| Upload cap / types | `MAX_UPLOAD_BYTES` / `ALLOWED_CONTENT_TYPES` | any |

**Content-addressing** dedups identical files to one blob; **ref-counting** makes
delete safe across sharers; **signed URLs** grant time-boxed access without a token.

_When every box is ✅, file-service is live. Next Tier-1: authz._
