# M03 — Catalog (Knowledge Bases, Documents, Chunks)

| | |
| --- | --- |
| **Package** | `cairn.catalog` |
| **Layer** | L3 control plane |
| **Phase** | 2 |
| **Owner** | Backend eng. B |
| **Depends on** | M00, M02 (facade), M04, M05, M06, M08, M10 (facade), M15 (facade) |
| **Depended on by** | M07 ingestion, M11, M14, apps/api. **Not by M09** (data plane reads cached DTOs) |
| **Tables owned** | `knowledge_base`, `kb_index_version`, `document`, `chunk`, `storage_binding` |
| **Requirements owned** | FR-C-01..11, FR-D-01..10, FR-F-09, FR-G-08, FR-I-12 |

---

## 1. Purpose and scope

The system of record for what knowledge exists.

**In scope:** KB lifecycle and configuration, storage bindings, document registration and
lifecycle, chunk storage and manual editing, index-version management and the blue/green
switch, KB config publication to the data-plane cache.

**Out of scope:** producing chunks (M07), embedding (M08), searching (M09), physical storage
(M04/M05).

---

## 2. Domain model

```python
@dataclass(frozen=True)
class KnowledgeBaseView:
    id: UUID; workspace_id: UUID
    name: str; slug: str; description: str | None; icon_url: str | None
    embedding_model_id: UUID; embedding_dim: int; metric: str
    vector_binding_id: UUID; object_binding_id: UUID
    chunk_config: ChunkConfig
    retrieval_config: RetrievalConfig
    active_index_version: int | None
    building_index_version: int | None
    config_version: int
    owner_user_id: UUID
    status: Literal["active","indexing","error","deleting","archived"]
    doc_count: int; chunk_count: int; bytes_used: int
    last_indexed_at: datetime | None

@dataclass(frozen=True)
class KnowledgeBaseRuntime:
    """The MINIMAL projection the data plane needs. Published to Redis, never an ORM object.
    Adding a field here is an interface change requiring M09 owner sign-off."""
    id: UUID; workspace_id: UUID
    embedding_model_ref: ModelRef          # provider, model_key, dim, normalize
    metric: str
    vector_binding: VectorBindingRef       # driver + connection config (no secrets)
    namespace: Namespace                   # (kb_id, ACTIVE index_version)
    retrieval_config: RetrievalConfig
    retrieval_pipeline: PipelineGraph | None
    config_version: int
    status: str
```

### Invariants

| # | Invariant | Enforced where |
| --- | --- | --- |
| I1 | `embedding_model_id`/`dim`/`metric` immutable once indexed | API + service + **DB trigger** (ADR-0006) |
| I2 | `embedding_dim` equals the referenced model's dimension | Service on create |
| I3 | At most one `building_index_version` per KB | Partial unique index |
| I4 | Retrieval reads **only** `active_index_version` | `Namespace` construction is a single function |
| I5 | `(kb_id, content_hash)` unique — no duplicate documents | DB constraint (`FR-D-07`) |
| I6 | A KB in `deleting` accepts no reads or writes | Service guard + cache bump |
| I7 | Counters (`doc_count`, `chunk_count`, `bytes_used`) are eventually accurate | Incremental update + nightly reconcile |

---

## 3. Public interface

```python
class CatalogService:
    # --- knowledge base ---
    async def create_kb(self, actor: Principal, spec: CreateKbSpec) -> KnowledgeBaseView: ...
    async def get_kb(self, kb_id: UUID) -> KnowledgeBaseView: ...
    async def list_kbs(self, workspace_id, actor, filters, page) -> CursorPage[KnowledgeBaseView]: ...
    async def update_kb(self, actor, kb_id, spec: UpdateKbSpec) -> KnowledgeBaseView: ...
    async def delete_kb(self, actor, kb_id) -> None: ...
    async def duplicate_kb(self, actor, kb_id, name: str) -> KnowledgeBaseView: ...
    async def transfer_ownership(self, actor, kb_id, new_owner_id) -> KnowledgeBaseView: ...
    async def set_icon(self, actor, kb_id, upload: UploadFile) -> str: ...

    # --- index versions (ADR-0007) ---
    async def start_reindex(self, actor, kb_id, spec: ReindexSpec) -> IndexVersionView: ...
    async def activate_index_version(self, kb_id: UUID, version: int) -> None: ...
    async def fail_index_version(self, kb_id: UUID, version: int, error: str) -> None: ...
    async def get_index_progress(self, kb_id: UUID) -> IndexProgressView: ...

    # --- data plane publication ---
    async def publish_runtime(self, kb_id: UUID) -> None:
        """Writes KnowledgeBaseRuntime to Redis under kb:runtime:{id}:{config_version}
        and publishes an invalidation message. Called on every config change."""

    # --- documents ---
    async def register_upload(self, actor, kb_id, upload: UploadSpec) -> DocumentView: ...
    async def register_bulk(self, actor, kb_id, uploads) -> list[DocumentRegistration]: ...
    async def get_document(self, doc_id: UUID) -> DocumentView: ...
    async def list_documents(self, kb_id, filters, page) -> CursorPage[DocumentView]: ...
    async def delete_document(self, actor, doc_id) -> None: ...
    async def retry_document(self, actor, doc_id) -> None: ...
    async def update_document_state(self, doc_id, state, *, stage=None,
                                    error_code=None, error_detail=None,
                                    progress=None) -> None:
        """Called by M07 workers to report progress."""

    # --- chunks ---
    async def list_chunks(self, doc_id, page) -> CursorPage[ChunkView]: ...
    async def get_chunk(self, chunk_id, kb_id) -> ChunkView: ...
    async def edit_chunk(self, actor, chunk_id, content: str) -> ChunkView: ...
    async def split_chunk(self, actor, chunk_id, at: int) -> list[ChunkView]: ...
    async def merge_chunks(self, actor, chunk_ids: list[UUID]) -> ChunkView: ...
    async def replace_chunks(self, doc_id, index_version, chunks: list[ChunkSpec]) -> None:
        """Bulk write from M07's chunk stage. Preserves is_edited chunks (FR-F-09)."""

    # --- storage bindings ---
    async def create_binding(self, actor, spec: BindingSpec) -> BindingView: ...
    async def list_bindings(self, workspace_id, kind=None) -> list[BindingView]: ...
    async def test_binding(self, binding_id) -> HealthStatus: ...
    async def delete_binding(self, actor, binding_id) -> None: ...
```

---

## 4. Behaviour

### 4.1 KB creation (`FR-C-01..06`)

```python
async def create_kb(self, actor, spec) -> KnowledgeBaseView:
    model = await self.modelgw.get_model(spec.embedding_model_id)
    if model.capability != "embedding":
        raise ValidationError("The selected model is not an embedding model.")
    if not model.is_enabled:
        raise ValidationError("The selected model is disabled.")

    vector_binding = await self._require_binding(spec.vector_binding_id, kind="vector")
    object_binding = await self._require_binding(spec.object_binding_id, kind="object")

    async with transaction() as session:
        kb = await self.repo.create(session, KnowledgeBase(
            workspace_id=actor.workspace_id,
            name=spec.name, slug=slugify(spec.name),
            embedding_model_id=model.id,
            embedding_dim=model.dimension,          # I2 — copied, not trusted from input
            metric=spec.metric,
            vector_binding_id=vector_binding.id,
            object_binding_id=object_binding.id,
            chunk_config=spec.chunk_config or ChunkConfig.default(),
            retrieval_config=spec.retrieval_config or RetrievalConfig.default(),
            owner_user_id=actor.id,
            active_index_version=None,              # nothing indexed yet
        ))
        # Creator gets full control; without this the creator cannot use their own KB.
        await self.authz.grant(session, GrantSpec(
            subject_type="user", subject_id=actor.id,
            resource_type="knowledge_base", resource_id=kb.id,
            permissions=["kb:read","kb:query","kb:write","kb:manage"]))
        await self.audit.record(session, action="kb.create", resource_id=kb.id)

    await self.publish_runtime(kb.id)
    return kb.to_view()
```

Version 1 of the namespace is allocated lazily on the first document, so an empty KB costs no
vector-store resources.

### 4.2 Immutability enforcement (`FR-C-04`, ADR-0006)

```python
IMMUTABLE_ONCE_INDEXED = {"embedding_model_id", "embedding_dim", "metric"}

async def update_kb(self, actor, kb_id, spec) -> KnowledgeBaseView:
    kb = await self.repo.get_for_update(kb_id)
    changed = spec.changed_fields()

    if kb.active_index_version is not None and (changed & IMMUTABLE_ONCE_INDEXED):
        raise EmbeddingModelImmutableError(
            "The embedding configuration cannot be changed for an indexed knowledge base. "
            "Use POST /v1/knowledge-bases/{id}/reindex to rebuild with a different model.")

    # Chunking changes are ALLOWED but require a reindex to take effect.
    requires_reindex = bool(changed & {"chunk_config"})
    ...
    if requires_reindex:
        result.reindex_required = True   # surfaced in the response so the UI can prompt
```

A database trigger (`trg_kb_embedding_immutable`) is the backstop — no future code path can
bypass this, reviewed or not.

### 4.3 Blue/green reindex (`FR-G-08`, ADR-0007)

```python
async def start_reindex(self, actor, kb_id, spec) -> IndexVersionView:
    async with transaction() as session:
        kb = await self.repo.get_for_update(session, kb_id)
        if kb.building_index_version is not None:
            raise ConflictError("A reindex is already in progress for this knowledge base.")

        if spec.embedding_model_id:               # the ONLY way to change the model
            model = await self.modelgw.get_model(spec.embedding_model_id)
            kb.embedding_model_id, kb.embedding_dim = model.id, model.dimension
        if spec.chunk_config:
            kb.chunk_config = spec.chunk_config

        new_version = (kb.active_index_version or 0) + 1
        await self._check_quota_for_double_storage(session, kb)   # peak is ~2×
        kb.building_index_version = new_version
        kb.status = "indexing"
        await self.repo.create_index_version(session, kb_id, new_version, state="building")

        # Transactional enqueue — the fan-out task cannot be lost (NFR-R-03)
        await self.tasks.enqueue(session, Task(
            queue="maintain", kind="reindex.fanout",
            workspace_id=kb.workspace_id, kb_id=kb_id,
            payload={"index_version": new_version}, priority=50))
    return ...
```

`reindex.fanout` walks documents in batches and enqueues `chunk`/`embed`/`index` tasks targeting
the new namespace. Retrieval keeps serving `active_index_version` throughout.

```python
async def activate_index_version(self, kb_id, version) -> None:
    async with transaction() as session:
        kb = await self.repo.get_for_update(session, kb_id)
        old = kb.active_index_version
        kb.active_index_version   = version
        kb.building_index_version = None
        kb.status                 = "active"
        kb.config_version        += 1                      # invalidates the data-plane cache
        kb.last_indexed_at        = utcnow()
        await self.repo.set_index_state(session, kb_id, version, "active")
        if old is not None:
            await self.repo.set_index_state(session, kb_id, old, "retired",
                                            retire_after=utcnow() + timedelta(hours=24))
            await self.tasks.enqueue(session, Task(
                queue="maintain", kind="index.drop", kb_id=kb_id,
                payload={"index_version": old},
                run_after=utcnow() + timedelta(hours=24)))
    await self.publish_runtime(kb_id)      # atomic switch visible to the data plane
```

### 4.4 Runtime publication — the data plane's only view of a KB

```python
async def publish_runtime(self, kb_id) -> None:
    kb = await self.repo.get(kb_id)
    runtime = KnowledgeBaseRuntime(
        id=kb.id, workspace_id=kb.workspace_id,
        embedding_model_ref=await self.modelgw.get_ref(kb.embedding_model_id),
        metric=kb.metric,
        vector_binding=await self._binding_ref(kb.vector_binding_id),
        namespace=Namespace(kb.id, kb.active_index_version),   # I4 — ACTIVE only
        retrieval_config=kb.retrieval_config,
        retrieval_pipeline=await self._graph_or_none(kb.retrieval_pipeline_id),
        config_version=kb.config_version, status=kb.status,
    )
    await self.cache.set(f"kb:runtime:{kb.id}", runtime.model_dump_json(), ttl=300)
    await self.cache.publish("kb:invalidate", str(kb.id))   # immediate, for delete/switch
```

> This is the seam that keeps ADR-0002 honest. M09 never sees a `KnowledgeBase` — it sees a
> `KnowledgeBaseRuntime` from Redis. Adding a field here is an interface change requiring the
> M09 owner's sign-off, which is exactly the friction that stops the boundary eroding.

### 4.5 Document registration and deduplication (`FR-D-07`)

```python
async def register_upload(self, actor, kb_id, upload) -> DocumentView:
    kb = await self._require_kb_writable(kb_id, actor)

    content_hash = sha256_stream(upload.stream)                    # streamed, never buffered
    existing = await self.repo.get_by_hash(kb_id, content_hash)
    if existing and existing.deleted_at is None:
        return existing.to_view(skipped_reason="DUPLICATE_CONTENT_HASH")  # FR-D-07

    mime = sniff_mime(upload.head_bytes)                            # content, not extension
    if mime not in SUPPORTED_MIME_TYPES:
        raise ValidationError(f"Unsupported file type: {mime}")
    if upload.size > kb.workspace_max_upload_bytes:
        raise ValidationError("File exceeds the maximum upload size.")

    object_key = f"{kb.workspace_id}/{kb_id}/originals/{content_hash}"
    await self.objectstore.put(kb.object_binding_id, object_key, upload.stream)

    async with transaction() as session:
        doc = await self.repo.create_document(session, Document(
            kb_id=kb_id, source_type="upload", source_ref=upload.filename,
            title=upload.filename, mime_type=mime, size_bytes=upload.size,
            content_hash=content_hash, object_key=object_key,
            metadata=upload.metadata, state="registered"))
        # SAME TRANSACTION as the document row — NFR-R-03
        await self.tasks.enqueue(session, Task(
            queue="parse", kind="document.parse",
            workspace_id=kb.workspace_id, kb_id=kb_id, document_id=doc.id,
            correlation_id=current_request_id(),
            dedupe_key=f"parse:{doc.id}:{doc.revision}"))
    return doc.to_view()
```

### 4.6 Chunk edits surviving reindex (`FR-F-09`)

```python
async def replace_chunks(self, doc_id, index_version, chunks) -> None:
    """M07 calls this after chunking. Manually edited chunks must not be silently discarded —
    a user who fixed a bad table extraction would lose that work on every reindex."""
    async with transaction() as session:
        edited = await self.repo.get_edited_chunks(session, doc_id)
        edited_by_ordinal = {c.ordinal: c for c in edited}

        await self.repo.delete_chunks(session, doc_id, index_version)
        for spec in chunks:
            if (preserved := edited_by_ordinal.get(spec.ordinal)) is not None:
                spec = spec.with_content(preserved.content, is_edited=True)
            await self.repo.insert_chunk(session, spec.for_version(index_version))
```

If chunking configuration changed, ordinals no longer align; in that case edits are preserved
as orphaned records and the user is warned in the reindex report rather than losing them.

### 4.7 KB deletion (`FR-C-08`) — order matters

```
1. status='deleting'; config_version++; publish_runtime   → retrieval stops immediately
2. enqueue maintain task kb.purge
3. purge, in this exact order:
     a. drop every vector namespace for the KB
     b. delete object-store artifacts by prefix
     c. delete chunk rows (partition-aware batches)
     d. delete document, crawl_url, crawl_run, crawl_job rows
     e. delete grants referencing the KB
     f. delete the knowledge_base row
```

Storage first, metadata last: a crash mid-purge leaves metadata pointing at deleted storage,
which is recoverable and detectable. The reverse leaves orphaned storage nobody can find.

---

## 5. API endpoints owned

| Method | Path | Permission |
| --- | --- | --- |
| GET/POST | `/v1/knowledge-bases` | `kb:read` / `kb:create` |
| GET/PATCH/DELETE | `/v1/knowledge-bases/{id}` | `kb:read` / `kb:manage` |
| POST | `/v1/knowledge-bases/{id}/icon` | `kb:manage` |
| POST | `/v1/knowledge-bases/{id}/reindex` | `kb:manage` |
| GET | `/v1/knowledge-bases/{id}/index-progress` | `kb:read` |
| POST | `/v1/knowledge-bases/{id}/duplicate` | `kb:create` |
| POST | `/v1/knowledge-bases/{id}/transfer` | `kb:manage` |
| GET/POST | `/v1/knowledge-bases/{id}/documents` | `kb:read` / `kb:write` |
| POST | `/v1/knowledge-bases/{id}/documents/bulk` | `kb:write` |
| GET/DELETE | `/v1/documents/{id}` | `kb:read` / `kb:write` |
| POST | `/v1/documents/{id}/retry` | `kb:write` |
| GET | `/v1/documents/{id}/chunks` | `kb:read` |
| PATCH | `/v1/chunks/{id}` | `kb:write` |
| POST | `/v1/chunks/{id}/split` · `/v1/chunks/merge` | `kb:write` |
| GET/POST | `/v1/storage-bindings` | `platform:storage` |
| POST | `/v1/storage-bindings/{id}/test` | `platform:storage` |

---

## 6. Errors owned

| Code | HTTP | Meaning |
| --- | --- | --- |
| `EMBEDDING_MODEL_IMMUTABLE` | 409 | Attempt to change embedding config after indexing |
| `KB_INDEX_IN_PROGRESS` | 409 | Reindex already running |
| `KB_NOT_READY` | 409 | KB is `deleting` or `error` |
| `DUPLICATE_CONTENT_HASH` | 200* | Not an error — returned as `skipped_reason` |
| `UNSUPPORTED_FILE_TYPE` | 400 | After content sniffing |
| `FILE_TOO_LARGE` | 413 | |
| `STORAGE_BINDING_UNAVAILABLE` | 502 | Health check failed |
| `CHUNK_NOT_EDITABLE` | 409 | Chunk belongs to a non-active index version |

---

## 7. Performance requirements

| Operation | Budget |
| --- | --- |
| `create_kb` | < 200 ms |
| `register_upload` (excl. transfer) | < 150 ms |
| `list_documents` page of 50 | < 100 ms p95 |
| `list_chunks` page of 50 | < 120 ms p95 |
| `publish_runtime` | < 30 ms |
| `replace_chunks` for 500 chunks | < 2 s (bulk insert, not per-row) |

---

## 8. Test requirements

| ID | Test | Req |
| --- | --- | --- |
| TC-M03-01 | KB creation copies `embedding_dim` from the model, ignoring client input | I2 |
| TC-M03-02 | **Changing `embedding_model_id` after indexing is rejected at the API** | FR-C-04 |
| TC-M03-03 | **The same change is rejected by the DB trigger when the service is bypassed** | ADR-0006 |
| TC-M03-04 | Changing it before any index build succeeds | FR-C-04 |
| TC-M03-05 | Creator receives full grants on their KB | FR-C-01 |
| TC-M03-06 | Duplicate upload returns the existing document with a skip reason | FR-D-07 |
| TC-M03-07 | MIME sniffing rejects a `.pdf` that is actually a ZIP | FR-D-05 |
| TC-M03-08 | Document row and parse task commit atomically | NFR-R-03 |
| TC-M03-09 | **Reindex: retrieval serves the old version throughout** | FR-G-08 |
| TC-M03-10 | **Activation switches atomically; no request sees a mixed index** | ADR-0007 |
| TC-M03-11 | Failed reindex leaves the active version untouched | ADR-0007 |
| TC-M03-12 | Concurrent reindex is rejected | I3 |
| TC-M03-13 | Edited chunks survive reindex when ordinals align | FR-F-09 |
| TC-M03-14 | KB deletion removes vectors, objects, and rows in the specified order | FR-C-08 |
| TC-M03-15 | A `deleting` KB immediately rejects retrieval | I6 |
| TC-M03-16 | `publish_runtime` writes a `KnowledgeBaseRuntime` containing only the ACTIVE namespace | I4 |
| TC-M03-17 | Counters stay accurate across add/delete/reindex | I7 |

Coverage target: **85%**.

---

## 9. Task breakdown

| Task | Description | Est (d) | Deps |
| --- | --- | --- | --- |
| T-M03-01 | ORM + migration: `knowledge_base`, `kb_index_version`, `document`, `chunk` (16 hash partitions), `storage_binding` | 1.5 | T-M00-05 |
| T-M03-02 | **`guard_kb_embedding_immutable` trigger + migration** | 0.5 | T-M03-01 |
| T-M03-03 | `ChunkConfig` / `RetrievalConfig` Pydantic schemas + defaults | 0.5 | |
| T-M03-04 | KB CRUD service | 2.0 | T-M03-01 |
| T-M03-05 | Storage binding CRUD + health test | 1.0 | T-M04, T-M05 |
| T-M03-06 | **`publish_runtime` + `KnowledgeBaseRuntime` DTO** | 1.0 | T-M03-04 |
| T-M03-07 | Document registration, dedup, MIME sniffing, object upload | 1.5 | T-M04 |
| T-M03-08 | Bulk upload with per-file results | 1.0 | T-M03-07 |
| T-M03-09 | Document state reporting API for workers | 0.5 | T-M03-07 |
| T-M03-10 | Chunk storage, bulk replace, edit/split/merge | 1.5 | T-M03-01 |
| T-M03-11 | **Index version lifecycle: start / activate / fail / progress** | 1.5 | T-M03-04 |
| T-M03-12 | KB deletion purge task | 1.0 | T-M03-11 |
| T-M03-13 | Icon upload + signed URL | 0.5 | T-M04 |
| T-M03-14 | Routers for KB, documents, chunks, bindings | 2.0 | all |
| T-M03-15 | Tests TC-M03-01..17 | 2.5 | all |
| | **Total** | **18.5** | |
