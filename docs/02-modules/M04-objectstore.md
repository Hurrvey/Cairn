# M04 — Object Store Drivers

| | |
| --- | --- |
| **Package** | `cairn.objectstore` |
| **Layer** | L1 driver |
| **Phase** | 2 |
| **Owner** | Backend eng. B |
| **Depends on** | M00 only |
| **Depended on by** | M03, M07, M11, M12 |
| **Tables owned** | none |
| **Requirements owned** | FR-C-10, NFR-SEC-07 |

---

## 1. Purpose and scope

Binary storage for originals, parsed artifacts, icons, and exports, behind one driver interface.

**In scope:** put/get/delete/list, streaming, presigned URLs, multipart upload, health.
**Out of scope:** deciding *what* to store or how to name it (callers own key layout).

---

## 2. Public interface

```python
class ObjectStore(Protocol):
    async def put(self, key: str, data: AsyncIterator[bytes] | bytes,
                  *, content_type: str | None = None,
                  metadata: Mapping[str, str] | None = None) -> ObjectInfo: ...
    async def get(self, key: str) -> AsyncIterator[bytes]: ...
    async def get_bytes(self, key: str, *, max_bytes: int) -> bytes: ...
    async def head(self, key: str) -> ObjectInfo | None: ...
    async def delete(self, key: str) -> bool: ...
    async def delete_prefix(self, prefix: str) -> int: ...
    async def list(self, prefix: str, *, limit: int = 1000,
                   cursor: str | None = None) -> ObjectPage: ...
    async def presigned_get(self, key: str, ttl: timedelta) -> str: ...
    async def presigned_put(self, key: str, ttl: timedelta) -> str: ...
    async def health(self) -> HealthStatus: ...

class ObjectStoreRegistry:
    async def for_binding(self, binding_id: UUID) -> ObjectStore:
        """Resolves a storage_binding to a cached, connected driver."""
```

Drivers: `LocalObjectStore` (dev), `S3ObjectStore` (MinIO / S3 / OSS / COS — all S3 API),
`FakeObjectStore` (in-memory, owned by this module, used by every consumer's tests).

---

## 3. Key layout convention

Callers own keys, but this layout is normative so purge-by-prefix works:

```
{workspace_id}/{kb_id}/originals/{content_hash}
{workspace_id}/{kb_id}/parsed/{document_id}/{revision}/content.md
{workspace_id}/{kb_id}/parsed/{document_id}/{revision}/layout.json
{workspace_id}/{kb_id}/assets/{document_id}/{image_index}.png
{workspace_id}/{kb_id}/icon/{ulid}.{ext}
{workspace_id}/exports/{export_id}.jsonl
```

`delete_prefix("{workspace_id}/{kb_id}/")` must therefore remove everything a KB owns
(`FR-C-08` step b).

---

## 4. Behaviour

### 4.1 Streaming is mandatory

`put` and `get` accept and return async iterators. A 200 MB PDF must never be buffered in
memory — with 8 concurrent uploads that is 1.6 GB of RSS and an OOM-killed worker.

`get_bytes` exists for small known-size objects only and **requires** an explicit `max_bytes`;
it raises `ObjectTooLargeError` rather than allocating past the limit.

### 4.2 Presigned URLs (`NFR-SEC-07`)

Icons and downloadable originals are served by presigned URL, never proxied through the API and
never served from the application origin. Default TTL 15 minutes.

For `LocalObjectStore`, presigning is emulated with an HMAC-signed path served by a dedicated,
sandboxed static route — clearly documented as **development only**.

### 4.3 Idempotent writes

`put` with an existing key overwrites. Since keys are content-hash based for originals, this is
naturally idempotent — a retried upload writes identical bytes to the same key.

### 4.4 Failure semantics

- Connection failures raise `UpstreamError`, never a driver-native exception.
- `get` on a missing key raises `NotFoundError`.
- `delete` on a missing key returns `False` (not an error) — deletion is idempotent.
- Every operation has a timeout (default 30 s; 300 s for multipart upload).

---

## 5. Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `CAIRN_OBJECTSTORE__DEFAULT_DRIVER` | `local` in dev, `s3` in prod | |
| `CAIRN_OBJECTSTORE__LOCAL_PATH` | `/var/lib/cairn/objects` | |
| `CAIRN_OBJECTSTORE__PRESIGN_TTL_S` | `900` | |
| `CAIRN_OBJECTSTORE__MULTIPART_THRESHOLD` | `16777216` | 16 MB |
| `CAIRN_OBJECTSTORE__TIMEOUT_S` | `30` | |

Per-binding config (`storage_binding.config`) carries `endpoint`, `bucket`, `region`,
`path_style`; credentials live in `secret` (`NFR-SEC-02`).

---

## 6. Test requirements

A **shared conformance suite** (`tests/contract/objectstore/`) runs against every driver
including the fake. A driver is not done until it passes.

| ID | Test |
| --- | --- |
| TC-M04-01 | put → head → get round-trips bytes exactly |
| TC-M04-02 | Streaming put of 100 MB keeps RSS growth under 50 MB |
| TC-M04-03 | `get_bytes` beyond `max_bytes` raises without allocating |
| TC-M04-04 | `delete` of a missing key returns `False` |
| TC-M04-05 | `delete_prefix` removes all and only the matching keys |
| TC-M04-06 | `list` paginates correctly with a cursor |
| TC-M04-07 | Presigned GET works and expires |
| TC-M04-08 | Presigned URL cannot be modified to reach another key |
| TC-M04-09 | Multipart upload above threshold; resume after interruption |
| TC-M04-10 | Backend errors surface as `UpstreamError`, never driver-native |
| TC-M04-11 | `health()` reports unavailable when the backend is stopped |
| TC-M04-12 | Keys with unicode and spaces round-trip |

Coverage target: **85%**.

---

## 7. Acceptance criteria

- [ ] Conformance suite passes for `local`, `s3` (MinIO via testcontainers), and `fake`
- [ ] No path in the codebase buffers an entire object in memory
- [ ] `FakeObjectStore` is published for consumer tests
- [ ] Credentials never appear in logs or `repr()`

---

## 8. Task breakdown

| Task | Description | Est (d) |
| --- | --- | --- |
| T-M04-01 | `ObjectStore` Protocol, DTOs, errors | 0.5 |
| T-M04-02 | `LocalObjectStore` + HMAC presign emulation | 1.0 |
| T-M04-03 | `S3ObjectStore` (aioboto3), multipart, presign | 2.0 |
| T-M04-04 | `ObjectStoreRegistry` with connection caching | 0.5 |
| T-M04-05 | `FakeObjectStore` | 0.5 |
| T-M04-06 | Conformance suite TC-M04-01..12 | 1.5 |
| | **Total** | **6.0** |
