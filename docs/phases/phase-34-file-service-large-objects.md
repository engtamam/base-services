# Phase 34 — file-service carries both models: small-in-app and large-direct-to-bucket

> **Target release: v0.5.1.** Additive, behind the existing ports — a `local`-store deployment keeps
> working unchanged. This is a plug-and-play proof: the seams are already cut (the `ObjectStore`
> port, and a `SIGNED_URL_DRIVER=s3` builder that today literally raises *"a future adapter"*), so
> the large path is not a rewrite — it is filling a hole the original authors left, by contract.

file-service is smart engineering aimed at **documents, photos, exports, thumbnails**: the whole file
goes through the app (25 MiB cap), and on the way it is content-addressed (`key = sha256(bytes)` →
automatic dedup), type/virus-scanned, and ref-counted so a shared blob is deleted only when its last
owner is. Its "signed URL" is an HMAC token gating *our own* endpoint, not a presigned bucket URL.

That model is exactly right for small files and exactly wrong for a multi-GB video: you cannot hold
it in memory, you cannot scan it in the request, and you should not stream its bytes through the app
when the bucket can serve them directly. A backup archive or a raw video needs the **presigned,
direct-to-bucket, multipart** path.

So file-service should carry **both**, and choose by the wired store's capability — never by a
hardcoded branch. Small stays in-app. Large gets a presigned ticket, uploads straight to the bucket,
and is scanned asynchronously before it is readable.

---

## The invariant that makes this safe

**A file is never readable until it has been scanned, whichever path it took.**

The small path scans in-request before it stores. The large path cannot — so a large upload lands in
a `pending` state, an async worker scans it, and only a clean result flips it to `ready`. A `pending`
or `quarantined` file 404s to everyone (existence hidden, same as a cross-tenant file today). The
bucket can serve bytes directly, but only for a key the service has already blessed and only through
a short-lived presigned GET the service minted — the client never names a bucket key.

```
small (≤ MAX_INLINE_BYTES)   POST /files            bytes → app → scan → sha256 → put → ready   (unchanged)
large (> MAX_INLINE_BYTES)   POST /files/uploads    declare {name, ct, size, sha256}
                             → presigned multipart PUT  (client → bucket, direct)
                             POST /files/uploads/{id}/complete  → verify checksum → enqueue scan → pending
                             (worker)               scan → ready | quarantined
download                     GET /{id}              large+presign-capable → 302 presigned GET; else stream inline
```

---

## The seams (why this is plug-and-play, not surgery)

Everything below already exists; the phase wires them, it does not invent them.

- **`ObjectStore` port** (`app/ports/object_store.py`) gains two *optional* methods,
  `presign_put(key, content_type, size) -> {url|parts, upload_id}` and
  `presign_get(key, filename) -> url`, plus a `supports_presign: bool` capability flag. Only the
  `s3` adapter implements them (aioboto3 is already a dep); `local`/`memory` return `supports_presign
  = False` and raise `Unsupported` if called. **The store's capability decides the path — no config
  branch names "s3".**
- **The `SIGNED_URL` registry already has the hole.** `container.py:102`,
  `_SIGNED_URL["s3"]` raises `"s3 presigned URLs are a future adapter — use SIGNED_URL_DRIVER=hmac"`.
  This phase replaces that stub with the real S3 presigner. HMAC (our-endpoint) links stay the default
  and the only option for the small/local path.
- **The `Queue` port is already wired** (`QUEUE_DRIVER`, `QUEUE_URL`). The async scan of a multi-GB
  object has a home the day this lands; no new infra.
- **Content-addressing survives, and gets *better* for large files.** The server cannot hash bytes it
  never sees, so the large path trusts the **client-declared sha256 as the key** and *verifies* it via
  S3's `x-amz-checksum-sha256` at `complete` (a mismatch quarantines). Dedup then falls out for free:
  if that sha is already ref-counted, the upload is **skipped entirely** — the client uploads nothing,
  we just `incr_ref`. Instant dedup of a 4 GB file is a feature the small path cannot match.

---

## 1. Definition of Done

### (1) Is this on the hot path?

Upload/download of large objects is I/O-bound but **not** a per-request fan-out, and the whole point
is that **the bytes no longer touch the app** — the app does three small metadata calls (initiate,
complete, presign-get) while the bucket moves the gigabytes. So the lever here is *not* caching; it is
**getting the app out of the data path**. The benchmark asserts exactly that: a large upload/download
does **O(1) app work independent of file size** — no `request.body()` on the large path, no
`object_store.get()` streaming through the app, memory bounded regardless of object size. The small
path keeps its existing behaviour and its existing benchmark.

### (2) Does every config knob have a consumer?

New knobs, each grepped to a consumer (the knob-guard gate enforces this):

- `MAX_INLINE_BYTES` (default = `MAX_UPLOAD_BYTES`, i.e. 25 MiB) — the boundary. `≤` → in-app; `>` →
  presign. Consumed by `FileService` to pick the path. When it equals the ceiling, behaviour is
  **identical to today** (large path never triggers) — a safe default.
- `MAX_OBJECT_BYTES` (default e.g. 5 GiB) — the hard ceiling for the *large* path; `initiate` refuses
  a declared size above it. `MAX_UPLOAD_BYTES` remains the ceiling for the *inline* path.
- `MULTIPART_PART_SIZE` (default 8 MiB) — part size for the presigned multipart plan.
- `PRESIGN_TTL_SECONDS` (default 900) — lifetime of a presigned PUT/GET; distinct from
  `SIGNED_URL_TTL_SECONDS` (the HMAC-link TTL).
- `LARGE_UPLOAD_SCAN` (`async` | `require` | `skip`, default `async`) — `require` fails initiation
  closed if no real scanner+queue is wired; `skip` is refused in production by a boot guard (a
  multi-GB unscanned public download is not a default we ship).
- `UPLOAD_SESSION_TTL_SECONDS` (default 3600) — a `pending` upload session that never completes is
  swept (its partial multipart aborted in the bucket, so it stops costing money). CronJob wired in
  deploy values, asserted by the chart guard.

### (3) What does production need that "add a big-file endpoint" omits?

- **Fail-closed capability negotiation.** Over `MAX_INLINE_BYTES` with a store whose
  `supports_presign` is `False` (e.g. `local`) → **413 with a clear reason**, never a silent fall back
  to buffering 5 GiB in the app. The manifest advertises whether large uploads are available so a
  client knows before it tries.
- **Checksum verification at `complete`.** The client-declared sha256 is *claimed*, not *trusted*,
  until S3's stored checksum confirms it. Mismatch → `quarantined`, ref not incremented. Otherwise a
  client could poison a content-addressed key and every future dedup would serve their bytes.
- **Abort/cleanup of abandoned multiparts.** An incomplete S3 multipart upload holds parts that
  accrue storage cost forever. The session sweeper aborts them; `delete` of a `pending` file aborts
  the multipart, not just the metadata row.
- **The DSAR / erasure path must cover both.** `core/erasure.py` deletes by sha; a large blob and its
  in-flight multipart must both be erasable, and the erasure receipt must not claim success for an
  object still mid-upload. (The s3 `delete()` "was it really there" bug from Phase 28 is the cautionary
  tale — the receipt must be true.)
- **Ref-count semantics unchanged.** Large blobs join the same ref-count table; a large file shared by
  two owners is stored once and deleted at the last owner, exactly like a small one.
- **Range requests / resumable download** come for free once download is a presigned GET — the bucket
  honours `Range`. The app no longer has to.

### (4) The correctness footgun

**Marking a file `ready` (or advancing dedup ref-count) before the bytes are actually in the bucket
and scanned.** The whole model breaks if `complete` optimistically flips state on the client's word.
So: `complete` HEADs the object (it exists, size matches), verifies the checksum, and only then
enqueues the scan; the file is `ready` **only** after a clean scan result is recorded. The ref-count
is incremented at the same clean transition, never at `initiate` — otherwise an abandoned upload
would pin a blob forever. A second footgun: **trusting the client-supplied bucket key**. The client
never supplies a key; the server derives it from the (verified) sha256, so a client cannot presign a
PUT over someone else's blob or a GET of a key it doesn't own.

---

## 2. Steps

| # | Step |
| --- | --- |
| 34.1 | **Port + capability.** Add `presign_put` / `presign_get` / `supports_presign` to the `ObjectStore` Protocol; implement in `s3.py` (multipart create + per-part presign + presigned GET; checksum-sha256 required); `local`/`memory` return `supports_presign=False`, raise `Unsupported`. Contract test parametrized over adapters. |
| 34.2 | **Replace the `_SIGNED_URL["s3"]` stub** with the real S3 presigner; keep `hmac` the default. Config: `PRESIGN_TTL_SECONDS`. |
| 34.3 | **`UploadSession` model + store** (`pending`/`ready`/`quarantined`/`aborted`, tenant, owner, declared sha/size/ct, multipart id + part etags). Keyset-listable, TTL-swept. |
| 34.4 | **`FileService` path split.** `begin_large_upload()` (validate size ≤ `MAX_OBJECT_BYTES`, type; **dedup short-circuit** if sha already ref'd; else presign a plan) and `complete_large_upload()` (HEAD + checksum verify → enqueue scan → ref-count on clean). Inline `upload()` unchanged, now gated by `MAX_INLINE_BYTES`. |
| 34.5 | **Async scan worker.** Consume the scan queue, stream the object from the store to the scanner in bounded chunks, flip `ready`/`quarantined`. `LARGE_UPLOAD_SCAN` policy + production boot guard. |
| 34.6 | **Routes.** `POST /files/uploads` (initiate), `POST /files/uploads/{id}/complete`, `GET /files/uploads/{id}` (status). `GET /{id}` → 302 presigned GET for large+presign-capable, else inline stream. Signed-link path mirrors it. `MAX_OBJECT_BYTES`, `MULTIPART_PART_SIZE`. |
| 34.7 | **Erasure/DSAR + delete** cover large blobs and abort in-flight multiparts; receipt stays truthful. Session sweeper + CronJob in deploy values. |
| 34.8 | **Manifest + admin_actions.** Advertise `large_uploads: available|unavailable` (from the wired store's `supports_presign`). Admin surface stays operator-only (still no `platform.*` mutations to add — the panel gains a read of upload sessions, at most). |
| 34.9 | **Hot-path benchmark** (large path = O(1) app work, memory bounded, no `request.body()` / no through-app streaming) + **knob-guard** for all six new settings + **capability-negotiation test** (large + `local` store → 413, never buffered). |
| 34.10 | **README + a worked client example** (initiate → multipart PUT → complete → download-redirect), and the OpenAPI for the three new endpoints. |

---

## 3. Out of scope

- **Resumable/tus-protocol uploads.** S3 multipart already lets a client retry a failed part; a full
  tus server is a separate ask.
- **On-the-fly transforms** (thumbnailing, transcoding). That is a media-processing service that
  *consumes* file events, not file-service's job.
- **Cross-region replication / lifecycle tiering.** Bucket-level concerns, configured on the bucket,
  not modelled here.
- **A non-S3 presign adapter.** GCS/Azure speak different presign APIs; add them as adapters when a
  user needs one. The port is ready; we do not build speculative adapters (same rule that kept
  `opensearch`/`meili` out of search until needed).

## 4. What this design refuses to do

- **Refuses to buffer a large object in the app "just to keep one code path".** Over the cap without a
  presign-capable store is a 413, not a silent 5 GiB in memory. One model per size, chosen by
  capability.
- **Refuses to trust the client's checksum or key.** The sha is verified against the bucket's stored
  checksum before it becomes a content-addressed key; the key is server-derived.
- **Refuses to serve an unscanned large file.** `pending` is invisible; only a clean scan makes bytes
  reachable. `LARGE_UPLOAD_SCAN=skip` cannot be the production default.
- **Refuses to break the small path.** Default `MAX_INLINE_BYTES = MAX_UPLOAD_BYTES` means a
  deployment that changes nothing behaves exactly as v0.5.0.

---

## 5. Threat model — a direct-to-bucket path widens the attack surface

### T1 — Presigned-URL abuse / key confusion (CWE-639, CWE-918) — HIGH
A presigned PUT is a bearer capability to write one bucket key. If the client chose the key, it could
write over another tenant's blob, or presign a GET for a key it does not own. **Control:** the client
never supplies a key; the server derives it from the verified sha256, scopes the session to the
caller's tenant/owner, and mints presigned URLs only for that derived key, TTL-bounded
(`PRESIGN_TTL_SECONDS`).

### T2 — Content-address poisoning via a lying checksum (CWE-345) — HIGH
Content-addressing means the key *is* the content hash; if a client uploads bytes whose real hash ≠
the declared sha, every future dedup on that sha serves poisoned bytes. **Control:** `complete`
verifies S3's `x-amz-checksum-sha256` against the declared sha before the key is trusted or ref-counted;
mismatch → `quarantined`, no ref, no dedup.

### T3 — Malware through the async gap (CWE-434) — HIGH
The large path cannot scan in-request, so there is a window where clean-looking metadata exists for
unscanned bytes. **Control:** the file is `pending` and unreadable during that window; only a clean
async scan flips it `ready`; a reject quarantines. Download of `pending` 404s.

### T4 — Abandoned multiparts as a cost/DoS vector (CWE-400) — MEDIUM
A client that initiates many large uploads and never completes them leaves paid-for parts in the
bucket. **Control:** `UPLOAD_SESSION_TTL_SECONDS` sweeper aborts stale multiparts; per-tenant rate
limits on `initiate`; `delete` of a `pending` file aborts its multipart.

### T5 — SSRF via a caller-influenced endpoint (CWE-918) — MEDIUM
Same guard file-service already relies on: the S3 endpoint/bucket come from **config only**
(`OBJECTSTORE_ENDPOINT_URL`), never from a request, so a caller cannot redirect presign at an internal
address. Unchanged by this phase, restated because presign makes the endpoint client-visible.

### T6 — Erasure that lies about a large object (CWE-459) — MEDIUM
A DSAR must delete both the blob and any in-flight multipart, and the receipt must not claim a delete
that did not happen (the Phase-28 s3 `delete()` bug). **Control:** erasure aborts multiparts and uses
the checked `delete()` that reports true presence; the receipt reflects the real outcome.

---

## 6. The gate

No new dedicated CI gate; this phase is covered by the gates that already exist, and the DoD adds the
tests that make them bite:

- **`coverage:services`** already requires every service to carry a hot-path benchmark, a knob guard,
  and a probe contract — 34.9 extends file-service's to the large path.
- **The knob-guard** catches any of the six new settings shipping without a consumer (the exact defect
  class from [[phase-doc-rules]]).
- **The chart guard** asserts the new session-sweeper CronJob exists in deploy values, as it does for
  every other retention/sweep endpoint.
- **`contracts:postgres`** / the ObjectStore contract test gains the presign parametrization so the
  `s3` adapter's new methods are proven against a real MinIO, not just asserted.
