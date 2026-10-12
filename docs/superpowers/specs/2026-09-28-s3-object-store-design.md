# S3-Compatible Object Store Driver (T-M04-03) — Design

## September 29 implementation resolutions

The persistent advisor approved connection-time protection using aiobotocore's aiohttp resolver
and socket_factory hooks: validate DNS answers and each actual socket IP, preserve TLS/SigV4
hostnames, disable environment proxies, require explicit credentials for custom endpoints and
reject cross-origin redirects. `allow_private` is an explicit administrator opt-in. A one-time
DNS check is insufficient. Use aiobotocore >=3.9.1, whose connector supports these hooks.

Client caches compare binding configuration and sealed credential fingerprints on every use,
with a per-registry creation lock. Failed clients close immediately; replaced clients retire
until registry shutdown so in-flight streams are not closed during credential rotation.
NoSuchBucket is never classified as a missing object. Implement driver contracts first, then
encrypted credential facade/catalog wiring, UI and optional MinIO deployment, then live acceptance.

**Date:** 2026-09-28 · **Status:** Draft for independent review · **Spec:** M04 (`docs/02-modules/
M04-objectstore.md`); the registry currently refuses `s3`/`minio`/`oss`/`cos` with "not
implemented yet (T-M04-03)".

## 1. Driver

`cairn.objectstore.s3.S3ObjectStore` on **aiobotocore** (the async botocore client aioboto3 wraps;
M04 named aioboto3 — we need only the client layer, not the resource layer, so the smaller
dependency). One client per binding, created by the registry, closed with it.

| Operation | S3 call | Notes |
| --- | --- | --- |
| `put(bytes)` | `PutObject` | `ContentType`, user metadata (ASCII only; others base64-escaped) |
| `put(stream)` | multipart when the stream exceeds `multipart_threshold` (16 MiB): `CreateMultipartUpload` → `UploadPart` (16 MiB parts, one buffered part at a time) → `CompleteMultipartUpload`; `AbortMultipartUpload` on any failure | never buffers the whole object (TC-M04-02) |
| `get` | `GetObject`, `Body.iter_chunks(64 KiB)` | missing → `ObjectNotFound` |
| `get_bytes` | `HeadObject` size check, then bounded read | over → `ObjectTooLarge` |
| `head` | `HeadObject` | 404 → `None` |
| `delete` | `HeadObject` then `DeleteObject` | `False` when absent (S3 delete is always 204) |
| `delete_prefix` | `ListObjectsV2` pages + `DeleteObjects` (≤1000) | returns count; per-key errors raise |
| `list` | `ListObjectsV2` with `ContinuationToken` as the cursor | |
| `presigned_get` | `generate_presigned_url(get_object, ExpiresIn)` | TTL ≤ 7 days (SigV4 limit) |
| `health` | `HeadBucket` | `False` on any failure |

Keys are passed through unchanged (validated by the existing `ObjectKeys` rules); an optional
per-binding `prefix` is prepended and stripped, so one bucket can serve several deployments.

**Errors (same classification the vector store adopted, reviewed 2026-09-28):**
`NoSuchKey`/404 → `ObjectNotFound`; connection/timeouts, HTTP 5xx, `SlowDown`/`Throttling`/429 →
`ObjectStoreTransient(ObjectStoreUnavailable)` with `retryable = True`; every other client error
(`AccessDenied`, `NoSuchBucket`, `InvalidAccessKeyId`, `SignatureDoesNotMatch`, other 4xx) →
`ObjectStoreUnavailable` with `retryable = False`. Driver-native exceptions never escape
(TC-M04-10). Timeouts: connect 5 s, read `CAIRN_OBJECTSTORE__TIMEOUT_S` (30 s); botocore's own
retries limited to 2 (`standard` mode) so a task's backoff stays the outer loop.

## 2. Binding configuration and credentials

`storage_binding.config` for `driver ∈ {"s3", "minio", "oss", "cos"}` (all served by this driver):
`bucket` (required, 3–63 chars), `endpoint` (optional; required for non-AWS; `http(s)://host[:port]`,
no path/query/userinfo), `region` (default `us-east-1`), `path_style` (default `true` when an
endpoint is given, else `false`), `prefix` (optional, no leading `/`), `allow_private` (default
`false`; a private/loopback endpoint such as `http://minio:9000` requires it — same SSRF rule as
model providers). Unknown keys are refused.

Credentials: `access_key_id` + `secret_access_key`, **write-only**. Stored as one envelope-sealed
secret (`cairn.core.secrets`, purpose `storage_binding`, JSON `{"access_key_id","secret_access_key"}`)
referenced by the existing `storage_binding.secret_ref`. Responses expose only `has_credentials`.
`PUT /v1/storage-bindings/{id}/credentials` rotates (the old secret row is deleted in the same
transaction, the registry's cached client for that binding is dropped). Omitted credentials fall
back to the environment's default AWS chain (instance roles) only when `endpoint` is empty.

The `secret` table's ORM belongs to modelgw; catalog may not import it (import contract
`no_cross_orm_catalog`). A small facade `cairn.modelgw.secrets` (`store_secret`, `sealed_secret`,
`delete_secret`, each taking the caller's session) is added; model providers keep their current
code path.

**Workers and the data path:** `BindingRef` gains `credential: SealedCredential | None` (sealed,
never plaintext). Every place that builds an `ObjectBindingRef` from a `BindingRef` passes it; the
registry opens it with `CAIRN_MASTER_KEY` when building the client. Nothing plaintext reaches Redis
or logs; `ObjectBindingRef.__repr__` already omits credentials, and the S3 store's `repr` shows only
endpoint and bucket.

## 3. Deployment

Compose gains an optional `minio` service (profile `s3`, pinned image, volume `miniodata`,
console on 127.0.0.1 only) and `.env.example` entries; ops guide: create a bucket, add an
"S3-compatible" object binding with the endpoint `http://minio:9000`, private networking on.
Local storage stays the default.

## 4. Web

Storage page: object bindings get a driver choice **Local folder** / **S3-compatible**. The S3
form has endpoint, bucket, region, path-style, key prefix, private networking, access key ID and
secret key (password field); the binding menu offers "Replace credentials". `has_credentials`
shown as a badge. zh-CN/en-US strings.

## 5. Verification

- Conformance suite (`tests/contract/objectstore`) gains an `s3` parameter against MinIO when
  `CAIRN_TEST_S3_ENDPOINT` (+ key/secret/bucket) is set, else via testcontainers MinIO, else skip;
  plus presign expiry (TC-M04-07/08), multipart above threshold with abort on failure (TC-M04-09),
  unicode keys (TC-M04-12), health on a stopped backend (TC-M04-11).
- Unit: error classification with botocore stubs; config validation (unknown keys, private
  endpoint without opt-in, bucket names); credentials never in `repr`, logs or responses.
- Integration: create an S3 binding through the API with credentials → KB on it → upload → index
  → retrieve → document content via `/v1/documents/{id}/content` → rotate credentials → still
  works; the sealed secret is not readable in PostgreSQL or Redis.
- Full backend suite, web gate, Compose stack with `--profile s3` browser/API acceptance.

## Out of scope

Presigned PUT for browser-direct uploads (uploads stay proxied through the API with streaming),
server-side encryption options, lifecycle rules.
