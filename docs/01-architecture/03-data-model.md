# Data Model

**Document:** `01-architecture/03-data-model.md`
**Status:** Normative — the single source of truth for schema
**Date:** 2026-08-28

Module specs reference these tables; they do not redefine them. Any schema change is made
here first, then in an Alembic migration, then in the owning module spec.

---

## 1. Global conventions

| Rule | Detail |
| --- | --- |
| Naming | Singular snake_case tables; `id` primary keys; `<entity>_id` foreign keys |
| Internal keys | `UUID` (v7 where available, for index locality) |
| Public IDs | Prefixed ULID strings in the API, mapped from UUID — `kb_01H…`. See [conventions §2](06-cross-cutting-conventions.md) |
| Tenancy | **Every table carries `workspace_id`** (`NFR-S-04`). No exceptions except `workspace` itself and `system_bootstrap`. |
| Timestamps | `TIMESTAMPTZ`, UTC, `created_at` / `updated_at` on all mutable entities |
| Soft delete | `deleted_at TIMESTAMPTZ NULL` on user-visible resources; hard delete runs asynchronously |
| Enums | `TEXT` + `CHECK` constraint, not native PG enums (migration friction) |
| JSON | `JSONB`, always with a documented Pydantic schema; never a dumping ground |
| Money/tokens | `BIGINT` counts; never floats |
| Version columns | `*_version INT` monotonic counters used for cache-key invalidation |

---

## 2. Entity relationship overview

```
workspace ─┬─ user ─────────┬─ session
           │                ├─ api_key ────┐
           │                └─ resource_grant (subject)
           │
           ├─ storage_binding ──┐
           ├─ model_provider ── │ ─ model
           │                    │
           ├─ knowledge_base ───┴──┬─ document ── document_revision
           │                       │      │
           │                       │      └─ chunk ── (vectors → vector store)
           │                       ├─ crawl_job ── crawl_run
           │                       └─ kb_index_version
           │
           ├─ pipeline ── pipeline_version
           ├─ function ── function_version
           ├─ golden_set ── golden_item
           ├─ eval_run ── eval_result
           ├─ task
           ├─ audit_log
           └─ usage_record
```

---

## 3. DDL

### 3.1 Bootstrap and tenancy

```sql
-- Singleton table guarding first-boot initialization (FR-A-02).
CREATE TABLE system_bootstrap (
    id              SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    initialized_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    schema_version  TEXT        NOT NULL,
    instance_id     UUID        NOT NULL
);

CREATE TABLE workspace (
    id          UUID PRIMARY KEY,
    name        TEXT NOT NULL,
    slug        TEXT NOT NULL UNIQUE,
    settings    JSONB NOT NULL DEFAULT '{}',   -- WorkspaceSettings schema
    quota       JSONB NOT NULL DEFAULT '{}',   -- WorkspaceQuota schema
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

`workspace.settings` (Pydantic `WorkspaceSettings`):

```jsonc
{
  "admin_content_access": "break_glass",   // always | on_grant | break_glass   FR-B-09
  "break_glass_ttl_minutes": 60,
  "password_policy": { "min_length": 12, "require_classes": 3, "history": 1 },
  "session_ttl_minutes": 480,
  "max_upload_bytes": 209715200,
  "crawler_respect_robots": true,
  "default_embedding_model_id": null
}
```

### 3.2 Identity — owned by M01

```sql
CREATE TABLE "user" (
    id                   UUID PRIMARY KEY,
    workspace_id         UUID NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
    username             TEXT NOT NULL,
    email                TEXT,
    display_name         TEXT,
    password_hash        TEXT NOT NULL,                         -- argon2id, FR-A-08
    role                 TEXT NOT NULL CHECK (role IN ('admin','user')),

    -- forced credential change (FR-A-04..07). Durable => survives restart.
    must_change_password BOOLEAN NOT NULL DEFAULT false,
    password_changed_at  TIMESTAMPTZ,
    credential_version   INT NOT NULL DEFAULT 1,                -- FR-A-13

    is_active            BOOLEAN NOT NULL DEFAULT true,
    failed_login_count   INT NOT NULL DEFAULT 0,
    locked_until         TIMESTAMPTZ,
    last_login_at        TIMESTAMPTZ,
    perm_version         INT NOT NULL DEFAULT 1,                -- FR-B-12 cache key
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at           TIMESTAMPTZ,
    CONSTRAINT uq_user_username UNIQUE (workspace_id, username)
);
CREATE INDEX ix_user_workspace_active ON "user"(workspace_id) WHERE deleted_at IS NULL;

CREATE TABLE session (
    id                 UUID PRIMARY KEY,
    workspace_id       UUID NOT NULL,
    user_id            UUID NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
    token_hash         TEXT NOT NULL UNIQUE,          -- sha256 of the opaque token
    credential_version INT  NOT NULL,                 -- stale => session invalid
    scopes             TEXT[] NOT NULL DEFAULT '{}',  -- ['credential:bootstrap'] for change tokens
    ip                 INET,
    user_agent         TEXT,
    expires_at         TIMESTAMPTZ NOT NULL,
    revoked_at         TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at       TIMESTAMPTZ
);
CREATE INDEX ix_session_user ON session(user_id) WHERE revoked_at IS NULL;
CREATE INDEX ix_session_expiry ON session(expires_at) WHERE revoked_at IS NULL;
```

> The scope-limited change token issued by `FR-A-04` is a `session` row with
> `scopes = ['credential:bootstrap']` and a 10-minute expiry. It is **not** a normal session:
> middleware refuses every route outside the allowlist.

### 3.3 Authorization — owned by M02

```sql
CREATE TABLE api_key (
    id             UUID PRIMARY KEY,
    workspace_id   UUID NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
    owner_user_id  UUID NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,
    key_prefix     TEXT NOT NULL,                    -- 'cairn_sk_live_AbCd' — for lookup + display
    key_hash       TEXT NOT NULL UNIQUE,             -- sha256(full key). FR-B-13
    last_four      TEXT NOT NULL,
    scopes         TEXT[] NOT NULL DEFAULT '{}',     -- e.g. {'kb:query'}
    kb_ids         UUID[]  NOT NULL DEFAULT '{}',    -- empty = all KBs owner can access
    rate_limit_rpm INT,
    ip_allowlist   CIDR[],
    expires_at     TIMESTAMPTZ,
    revoked_at     TIMESTAMPTZ,
    last_used_at   TIMESTAMPTZ,
    use_count      BIGINT NOT NULL DEFAULT 0,
    created_by     UUID NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_api_key_prefix ON api_key(key_prefix) WHERE revoked_at IS NULL;
CREATE INDEX ix_api_key_owner  ON api_key(owner_user_id);

CREATE TABLE resource_grant (
    id            UUID PRIMARY KEY,
    workspace_id  UUID NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
    subject_type  TEXT NOT NULL CHECK (subject_type IN ('user','api_key')),
    subject_id    UUID NOT NULL,
    resource_type TEXT NOT NULL CHECK (resource_type IN
                     ('workspace','knowledge_base','pipeline','function','model','golden_set')),
    resource_id   UUID,                              -- NULL = workspace-wide for that type
    permissions   TEXT[] NOT NULL,                   -- {'kb:read','kb:query'}
    granted_by    UUID NOT NULL,
    reason        TEXT,                              -- required for break-glass, FR-B-10
    is_break_glass BOOLEAN NOT NULL DEFAULT false,
    expires_at    TIMESTAMPTZ,
    revoked_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_grant_subject ON resource_grant(subject_type, subject_id)
    WHERE revoked_at IS NULL;
CREATE INDEX ix_grant_resource ON resource_grant(resource_type, resource_id)
    WHERE revoked_at IS NULL;
```

### 3.4 Storage and models — owned by M03 / M10

```sql
CREATE TABLE storage_binding (
    id           UUID PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL CHECK (kind IN ('vector','object')),
    driver       TEXT NOT NULL,      -- pgvector|qdrant|elasticsearch | local|s3|oss
    name         TEXT NOT NULL,
    config       JSONB NOT NULL DEFAULT '{}',   -- non-secret: host, bucket, region
    secret_ref   UUID REFERENCES secret(id),    -- encrypted credentials
    is_default   BOOLEAN NOT NULL DEFAULT false,
    health_state TEXT NOT NULL DEFAULT 'unknown'
                 CHECK (health_state IN ('unknown','healthy','degraded','unavailable')),
    checked_at   TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_binding_name UNIQUE (workspace_id, kind, name)
);

-- Envelope-encrypted secrets. NFR-SEC-02. Never returned by any API.
CREATE TABLE secret (
    id            UUID PRIMARY KEY,
    workspace_id  UUID NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
    purpose       TEXT NOT NULL,        -- 'model_provider' | 'storage_binding' | 'webhook'
    ciphertext    BYTEA NOT NULL,       -- AES-256-GCM
    nonce         BYTEA NOT NULL,
    wrapped_dek   BYTEA NOT NULL,
    key_version   INT   NOT NULL,       -- for KEK rotation
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    rotated_at    TIMESTAMPTZ
);

CREATE TABLE model_provider (
    id           UUID PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    family       TEXT NOT NULL,   -- openai|anthropic|google|dashscope|deepseek|ollama|vllm|openai_compatible
    base_url     TEXT,
    secret_ref   UUID REFERENCES secret(id),
    config       JSONB NOT NULL DEFAULT '{}',   -- timeouts, concurrency, retries  FR-K-07
    is_enabled   BOOLEAN NOT NULL DEFAULT true,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_provider_name UNIQUE (workspace_id, name)
);

CREATE TABLE model (
    id            UUID PRIMARY KEY,
    workspace_id  UUID NOT NULL,
    provider_id   UUID NOT NULL REFERENCES model_provider(id) ON DELETE CASCADE,
    model_key     TEXT NOT NULL,      -- provider-side identifier
    display_name  TEXT NOT NULL,
    capability    TEXT NOT NULL CHECK (capability IN ('chat','embedding','rerank')),  -- FR-K-03
    dimension     INT,                -- embedding only; NOT NULL when capability='embedding'
    max_input_tokens INT,
    normalize     BOOLEAN NOT NULL DEFAULT true,
    cost_per_1k_input  NUMERIC(12,6),
    cost_per_1k_output NUMERIC(12,6),
    is_enabled    BOOLEAN NOT NULL DEFAULT true,
    health_state  TEXT NOT NULL DEFAULT 'unknown',
    checked_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_model UNIQUE (provider_id, model_key),
    CONSTRAINT ck_embedding_dim CHECK (capability <> 'embedding' OR dimension IS NOT NULL)
);
```

### 3.5 Knowledge base — owned by M03

```sql
CREATE TABLE knowledge_base (
    id            UUID PRIMARY KEY,
    workspace_id  UUID NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    slug          TEXT NOT NULL,
    description   TEXT,
    icon_object_key TEXT,                     -- FR-C-10
    metadata      JSONB NOT NULL DEFAULT '{}',

    -- IMMUTABLE after first successful index build (FR-C-04, ADR-0006)
    embedding_model_id UUID NOT NULL REFERENCES model(id),
    embedding_dim      INT  NOT NULL,
    metric             TEXT NOT NULL DEFAULT 'cosine'
                       CHECK (metric IN ('cosine','dot','l2')),

    vector_binding_id  UUID NOT NULL REFERENCES storage_binding(id),
    object_binding_id  UUID NOT NULL REFERENCES storage_binding(id),

    chunk_config     JSONB NOT NULL DEFAULT '{}',   -- ChunkConfig      FR-C-05
    retrieval_config JSONB NOT NULL DEFAULT '{}',   -- RetrievalConfig  FR-C-06

    ingest_pipeline_id    UUID REFERENCES pipeline(id),   -- FR-L-04
    retrieval_pipeline_id UUID REFERENCES pipeline(id),

    -- blue/green indexing (FR-G-08, ADR-0007)
    active_index_version   INT,
    building_index_version INT,

    owner_user_id UUID NOT NULL REFERENCES "user"(id),
    status        TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active','indexing','error','deleting','archived')),
    config_version INT NOT NULL DEFAULT 1,     -- cache-key invalidation

    doc_count     INT    NOT NULL DEFAULT 0,
    chunk_count   BIGINT NOT NULL DEFAULT 0,
    bytes_used    BIGINT NOT NULL DEFAULT 0,
    last_indexed_at TIMESTAMPTZ,

    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at    TIMESTAMPTZ,
    CONSTRAINT uq_kb_slug UNIQUE (workspace_id, slug)
);

CREATE TABLE kb_index_version (
    kb_id        UUID NOT NULL REFERENCES knowledge_base(id) ON DELETE CASCADE,
    version      INT  NOT NULL,
    workspace_id UUID NOT NULL,
    state        TEXT NOT NULL CHECK (state IN ('building','active','retired','failed')),
    layout       TEXT NOT NULL DEFAULT 'shared' CHECK (layout IN ('shared','dedicated')),
    physical_ref TEXT,                        -- collection/table name in the backend
    chunk_total  BIGINT NOT NULL DEFAULT 0,
    chunk_done   BIGINT NOT NULL DEFAULT 0,   -- FR-G-09 progress
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    retire_after TIMESTAMPTZ,
    error        TEXT,
    PRIMARY KEY (kb_id, version)
);
```

`chunk_config` (Pydantic `ChunkConfig`):

```jsonc
{
  "strategy": "parent_child",          // fixed|recursive|markdown|semantic|parent_child|custom
  "child_tokens": 512, "child_overlap": 64,
  "parent_tokens": 2048,
  "separators": ["\n## ", "\n### ", "\n\n", "。", ". "],
  "keep_tables_intact": true,
  "min_chunk_tokens": 32,
  "function_id": null, "function_version": null   // FR-M-02 custom chunker
}
```

`retrieval_config` (Pydantic `RetrievalConfig`):

```jsonc
{
  "search_mode": "hybrid",
  "fusion": { "method": "rrf", "k": 60 },
  "weights": { "dense": 0.7, "sparse": 0.3 },
  "top_k": 5, "candidate_k": 100,
  "score_threshold": 0.0,
  "rerank": { "enabled": true, "model_id": null, "top_n": 5 },
  "expand_parent": true,
  "dedupe": "none",                    // none|by_chunk|by_document
  "mmr": { "enabled": false, "lambda": 0.5 }
}
```

### 3.6 Documents and chunks — owned by M03 / M07

```sql
CREATE TABLE document (
    id            UUID PRIMARY KEY,
    workspace_id  UUID NOT NULL,
    kb_id         UUID NOT NULL REFERENCES knowledge_base(id) ON DELETE CASCADE,

    source_type   TEXT NOT NULL CHECK (source_type IN ('upload','crawl','s3_sync','api','connector')),
    source_ref    TEXT,                       -- filename or URL
    crawl_job_id  UUID REFERENCES crawl_job(id) ON DELETE SET NULL,

    title         TEXT,
    mime_type     TEXT,
    size_bytes    BIGINT,
    content_hash  TEXT NOT NULL,              -- FR-D-07 idempotency
    object_key    TEXT,                       -- original in object store
    parsed_object_key TEXT,                   -- normalized markdown + layout json

    metadata      JSONB NOT NULL DEFAULT '{}',  -- user metadata, FR-D-10

    state         TEXT NOT NULL DEFAULT 'registered' CHECK (state IN (
                    'registered','fetching','fetched','parsing','parsed',
                    'chunking','chunked','embedding','embedded','indexing',
                    'indexed','failed','skipped','deleting')),
    stage_detail  TEXT,
    error_code    TEXT,                        -- machine-readable, FR-P-05
    error_detail  TEXT,
    progress_pct  SMALLINT NOT NULL DEFAULT 0,

    revision      INT NOT NULL DEFAULT 1,      -- FR-D-08
    page_count    INT,
    chunk_count   INT NOT NULL DEFAULT 0,
    token_count   BIGINT,

    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    indexed_at    TIMESTAMPTZ,
    deleted_at    TIMESTAMPTZ,
    CONSTRAINT uq_doc_hash UNIQUE (kb_id, content_hash)      -- dedup within a KB
);
CREATE INDEX ix_document_kb_state ON document(kb_id, state) WHERE deleted_at IS NULL;
CREATE INDEX ix_document_source   ON document(kb_id, source_type, source_ref);

CREATE TABLE chunk (
    id            UUID NOT NULL,
    workspace_id  UUID NOT NULL,
    kb_id         UUID NOT NULL,
    document_id   UUID NOT NULL,
    index_version INT  NOT NULL,
    parent_id     UUID,                        -- NULL for parent/standalone chunks
    ordinal       INT  NOT NULL,

    content       TEXT NOT NULL,
    content_hash  TEXT NOT NULL,               -- incremental re-embed key, FR-G-06
    token_count   INT  NOT NULL,

    metadata      JSONB NOT NULL DEFAULT '{}', -- heading_path, page, source_url  FR-F-07
    is_edited     BOOLEAN NOT NULL DEFAULT false,   -- FR-F-09 manual edit survives reindex

    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (kb_id, id)
) PARTITION BY HASH (kb_id);
-- 16 partitions created in the baseline migration; see §5.

CREATE INDEX ix_chunk_document ON chunk(kb_id, document_id, index_version);
CREATE INDEX ix_chunk_hash     ON chunk(kb_id, content_hash);
```

> **Chunk text lives here AND in the vector store payload** (ADR-0005). Postgres is the source
> of truth; the vector-store copy is a derived read model that makes retrieval a single round
> trip. Writes always go through the indexer, never directly to the read model.

### 3.7 Crawling — owned by M07

```sql
CREATE TABLE crawl_job (
    id             UUID PRIMARY KEY,
    workspace_id   UUID NOT NULL,
    kb_id          UUID NOT NULL REFERENCES knowledge_base(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,
    seeds          TEXT[] NOT NULL,
    config         JSONB NOT NULL DEFAULT '{}',   -- CrawlConfig
    schedule_cron  TEXT,                          -- FR-E-06
    is_enabled     BOOLEAN NOT NULL DEFAULT true,
    next_run_at    TIMESTAMPTZ,
    last_run_at    TIMESTAMPTZ,
    created_by     UUID NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE crawl_run (
    id           UUID PRIMARY KEY,
    workspace_id UUID NOT NULL,
    crawl_job_id UUID NOT NULL REFERENCES crawl_job(id) ON DELETE CASCADE,
    state        TEXT NOT NULL CHECK (state IN ('running','completed','failed','cancelled')),
    stats        JSONB NOT NULL DEFAULT '{}',   -- fetched/added/updated/unchanged/skipped/failed  FR-E-11
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ,
    error        TEXT
);

-- Per-URL state enabling incremental recrawl (FR-E-07) and removal detection (FR-E-08).
CREATE TABLE crawl_url (
    crawl_job_id UUID NOT NULL REFERENCES crawl_job(id) ON DELETE CASCADE,
    url_hash     TEXT NOT NULL,
    url          TEXT NOT NULL,
    workspace_id UUID NOT NULL,
    document_id  UUID REFERENCES document(id) ON DELETE SET NULL,
    content_hash TEXT,
    etag         TEXT,
    last_modified TEXT,
    status       TEXT NOT NULL,   -- fetched|unchanged|skipped_robots|skipped_scope|failed|gone
    depth        SMALLINT NOT NULL DEFAULT 0,
    last_seen_run UUID,
    last_fetched_at TIMESTAMPTZ,
    PRIMARY KEY (crawl_job_id, url_hash)
);
```

### 3.8 Task queue — owned by M06

```sql
CREATE TABLE task (
    id            BIGSERIAL PRIMARY KEY,
    workspace_id  UUID NOT NULL,
    kb_id         UUID,
    document_id   UUID,
    correlation_id TEXT,                       -- X-Request-Id of the originating call

    queue         TEXT NOT NULL CHECK (queue IN
                    ('fetch','parse','ocr','chunk','embed','index','maintain')),
    kind          TEXT NOT NULL,
    payload       JSONB NOT NULL DEFAULT '{}',

    state         TEXT NOT NULL DEFAULT 'ready'
                  CHECK (state IN ('ready','running','done','failed','blocked','cancelled')),
    priority      SMALLINT NOT NULL DEFAULT 100,
    attempt       SMALLINT NOT NULL DEFAULT 0,
    max_attempts  SMALLINT NOT NULL DEFAULT 5,

    run_after     TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_until   TIMESTAMPTZ,
    worker_id     TEXT,

    dedupe_key    TEXT,
    error_code    TEXT,
    error_detail  TEXT,

    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ
);

-- The claim index. Partial => small and hot.
CREATE INDEX ix_task_claim ON task (queue, priority DESC, id)
    WHERE state = 'ready';
CREATE INDEX ix_task_lease ON task (lease_until)
    WHERE state = 'running';
CREATE UNIQUE INDEX uq_task_dedupe ON task (dedupe_key)
    WHERE dedupe_key IS NOT NULL AND state IN ('ready','running');
CREATE INDEX ix_task_document ON task (document_id) WHERE document_id IS NOT NULL;

-- Fair scheduling counter (see M06 §5). Updated in the same transaction as claim/finish.
CREATE TABLE workspace_runtime (
    workspace_id       UUID PRIMARY KEY REFERENCES workspace(id) ON DELETE CASCADE,
    running            INT NOT NULL DEFAULT 0,
    concurrency_limit  INT NOT NULL DEFAULT 16,
    CONSTRAINT ck_running_nonneg CHECK (running >= 0)
);
```

### 3.9 Pipelines and functions — owned by M11 / M12

```sql
CREATE TABLE pipeline (
    id             UUID PRIMARY KEY,
    workspace_id   UUID NOT NULL,
    name           TEXT NOT NULL,
    kind           TEXT NOT NULL CHECK (kind IN ('ingest','retrieval')),
    description    TEXT,
    current_version INT,
    created_by     UUID NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at     TIMESTAMPTZ,
    CONSTRAINT uq_pipeline_name UNIQUE (workspace_id, name)
);

CREATE TABLE pipeline_version (
    pipeline_id  UUID NOT NULL REFERENCES pipeline(id) ON DELETE CASCADE,
    version      INT  NOT NULL,
    workspace_id UUID NOT NULL,
    graph        JSONB NOT NULL,        -- PipelineGraph — nodes, edges, config
    state        TEXT NOT NULL CHECK (state IN ('draft','published','archived')),
    published_at TIMESTAMPTZ,
    created_by   UUID NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (pipeline_id, version)
);

CREATE TABLE function (
    id             UUID PRIMARY KEY,
    workspace_id   UUID NOT NULL,
    name           TEXT NOT NULL,
    slot           TEXT NOT NULL CHECK (slot IN ('parse','chunk','enrich','filter','rerank')),
    description    TEXT,
    current_version INT,
    created_by     UUID NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at     TIMESTAMPTZ,
    CONSTRAINT uq_function_name UNIQUE (workspace_id, name)
);

CREATE TABLE function_version (
    function_id   UUID NOT NULL REFERENCES function(id) ON DELETE CASCADE,
    version       INT  NOT NULL,
    workspace_id  UUID NOT NULL,
    source_code   TEXT NOT NULL,
    dependencies  TEXT[] NOT NULL DEFAULT '{}',   -- must be within the allowlist  FR-M-08
    limits        JSONB NOT NULL DEFAULT '{}',    -- cpu_ms, mem_mb, timeout_s
    egress_allowlist TEXT[] NOT NULL DEFAULT '{}',-- empty = no network  FR-M-04
    state         TEXT NOT NULL CHECK (state IN ('draft','published','archived')),
    created_by    UUID NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (function_id, version)
);
```

### 3.10 Evaluation — owned by M14

```sql
CREATE TABLE golden_set (
    id           UUID PRIMARY KEY,
    workspace_id UUID NOT NULL,
    kb_id        UUID NOT NULL REFERENCES knowledge_base(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    description  TEXT,
    item_count   INT NOT NULL DEFAULT 0,
    created_by   UUID NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE golden_item (
    id            UUID PRIMARY KEY,
    golden_set_id UUID NOT NULL REFERENCES golden_set(id) ON DELETE CASCADE,
    workspace_id  UUID NOT NULL,
    query         TEXT NOT NULL,
    relevant_chunk_ids UUID[] NOT NULL DEFAULT '{}',
    relevance     JSONB NOT NULL DEFAULT '{}',   -- optional graded relevance {chunk_id: 0..3}
    notes         TEXT,
    source        TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual','imported','generated')),
    reviewed      BOOLEAN NOT NULL DEFAULT false  -- FR-N-02 human review gate
);

CREATE TABLE eval_run (
    id            UUID PRIMARY KEY,
    workspace_id  UUID NOT NULL,
    golden_set_id UUID NOT NULL REFERENCES golden_set(id) ON DELETE CASCADE,
    label         TEXT NOT NULL,
    config        JSONB NOT NULL,          -- the RetrievalConfig / ChunkConfig under test
    temp_index_version INT,                -- cleaned up afterwards, FR-N-06
    state         TEXT NOT NULL CHECK (state IN ('queued','running','completed','failed')),
    metrics       JSONB NOT NULL DEFAULT '{}',  -- recall@k, mrr, ndcg@k, precision@k, latency
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    created_by    UUID NOT NULL
);

CREATE TABLE eval_result (
    id            UUID PRIMARY KEY,
    eval_run_id   UUID NOT NULL REFERENCES eval_run(id) ON DELETE CASCADE,
    golden_item_id UUID NOT NULL,
    workspace_id  UUID NOT NULL,
    retrieved     JSONB NOT NULL,       -- ranked chunk_ids with scores
    metrics       JSONB NOT NULL,       -- per-query metrics
    latency_ms    INT NOT NULL
);
```

### 3.11 Platform — owned by M15

```sql
CREATE TABLE audit_log (
    id            BIGSERIAL,
    workspace_id  UUID NOT NULL,
    actor_type    TEXT NOT NULL CHECK (actor_type IN ('user','api_key','system')),
    actor_id      UUID,
    actor_label   TEXT NOT NULL,            -- denormalized: survives actor deletion
    action        TEXT NOT NULL,            -- 'user.create', 'grant.revoke', 'break_glass.open'
    resource_type TEXT,
    resource_id   UUID,
    outcome       TEXT NOT NULL CHECK (outcome IN ('success','failure','denied')),
    ip            INET,
    user_agent    TEXT,
    request_id    TEXT,
    before        JSONB,
    after         JSONB,
    detail        JSONB NOT NULL DEFAULT '{}',
    at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (at, id)
) PARTITION BY RANGE (at);
-- monthly partitions, created by worker-maintain 2 months ahead.

CREATE INDEX ix_audit_actor    ON audit_log (workspace_id, actor_id, at DESC);
CREATE INDEX ix_audit_resource ON audit_log (workspace_id, resource_type, resource_id, at DESC);
CREATE INDEX ix_audit_action   ON audit_log (workspace_id, action, at DESC);

CREATE TABLE usage_record (
    id            BIGSERIAL,
    workspace_id  UUID NOT NULL,
    kb_id         UUID,
    principal_type TEXT NOT NULL,
    principal_id  UUID,
    kind          TEXT NOT NULL,    -- 'embedding'|'rerank'|'chat'|'retrieval'|'storage'
    model_id      UUID,
    units         BIGINT NOT NULL,  -- tokens, requests, or bytes depending on kind
    cost_micros   BIGINT,
    at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (at, id)
) PARTITION BY RANGE (at);

CREATE INDEX ix_usage_ws ON usage_record (workspace_id, kind, at DESC);
```

---

## 4. Vector store read model

Not SQL — the logical schema written to whichever backend the KB uses (ADR-0005).

```jsonc
// Point in namespace (kb_id, index_version)
{
  "id": "<chunk_id uuid>",
  "vectors": {
    "dense":  [0.013, -0.221, ...],           // embedding_dim floats
    "sparse": { "indices": [...], "values": [...] }   // BM25/SPLADE terms  FR-G-05
  },
  "payload": {
    "kb_id":        "<uuid>",       // indexed, tenant-partitioned key
    "document_id":  "<uuid>",       // indexed
    "index_version": 3,             // indexed
    "parent_id":    "<uuid|null>",
    "ordinal":      17,
    "content":      "Key rotation is performed via …",   // denormalized text
    "token_count":  418,
    "heading_path": ["Security", "Key Management"],
    "page":         14,
    "source_url":   "https://…",
    "created_at":   1735689600,
    "meta":         { "lang": "en", "tags": ["security"] }   // user metadata, filterable
  }
}
```

**Payload index requirements** (backend-specific, see [M05](../02-modules/M05-vectorstore.md)):
`kb_id` (keyword, tenant), `document_id` (keyword), `index_version` (integer),
`meta.*` (dynamic keyword/integer/float), `created_at` (integer range).

---

## 5. Partitioning

| Table | Scheme | Count | Trigger |
| --- | --- | --- | --- |
| `chunk` | HASH (`kb_id`) | 16 from day one | Avoids a later rewrite; hash keeps a KB's chunks co-located |
| `audit_log` | RANGE (`at`) monthly | rolling | Cheap retention drop (`FR-O-03`) |
| `usage_record` | RANGE (`at`) monthly | rolling | Same |
| `task` | none initially → RANGE (`created_at`) | — | Only if `task` exceeds ~50M rows; completed rows are pruned by `worker-maintain` |

`worker-maintain` creates future partitions two months ahead and drops expired ones.

---

## 6. Migration policy

1. **One migration per PR.** Never edit a merged migration.
2. **Forward-compatible with the previous release** (`NFR-R-09`) — enables rolling deploys.
3. **Expand → migrate → contract** for breaking changes, across three releases:
   - R1 add the new column nullable, dual-write
   - R2 backfill, switch reads
   - R3 drop the old column
4. **No long locks.** `CREATE INDEX CONCURRENTLY`; add `NOT NULL` via `CHECK NOT VALID` then `VALIDATE`.
5. **Data migrations are tasks, not migrations.** Alembic changes schema only; backfills run as `maintain` tasks with progress reporting.
6. Migrations run in the one-shot `migrate` container under `pg_advisory_lock` (`NFR-D-03`).

---

## 7. Retention and deletion

| Data | Default retention | Mechanism |
| --- | --- | --- |
| `audit_log` | 365 days | Partition drop |
| `usage_record` | 730 days | Partition drop |
| `task` (`done`) | 7 days | Batched delete by `worker-maintain` |
| `session` (expired) | 24 h | Batched delete |
| Retired `kb_index_version` | 24 h after switch | Namespace drop + row delete |
| Soft-deleted KB / document | 30 days | Then cascade hard delete as a `maintain` task |
| Object-store originals | Life of the document | Deleted with the document |

**KB deletion order** (`FR-C-08`) — must be exactly this, so a crash never orphans storage:
1. `status = 'deleting'`, config cache bumped (retrieval stops immediately)
2. Drop all vector namespaces
3. Delete object-store artifacts by prefix
4. Delete `chunk`, `document`, `crawl_*` rows
5. Delete the `knowledge_base` row

---

## 8. Sizing reference

| Entity | Row size (approx) | 100k docs | 1M docs |
| --- | --- | --- | --- |
| `document` | ~1 KB | 100 MB | 1 GB |
| `chunk` (300/doc, ~1.2 KB) | ~1.4 KB | 42 GB | 420 GB |
| `task` (transient) | ~0.5 KB | < 1 GB | < 5 GB |
| `audit_log` | ~1 KB | grows with activity | partition-dropped |
| Vectors, 1024-d fp32, HNSW m=16 | ~4.2 KB | 126 GB | 1.26 TB |
| Vectors, int8 quantized | ~1.1 KB | 33 GB | 330 GB |

**Planning rule:** a 16 GB Qdrant node comfortably serves ~10M vectors at int8 with rescoring
— roughly 30k–50k typical documents. Scalar quantization costs ~1% recall and should be the
default above 1M vectors. Reserve binary quantization for > 50M.
