# Control Plane API

**Document:** `03-api/03-control-plane-api.md`
**Status:** Normative
**Date:** 2026-08-28

Management endpoints. Conventions from [`01-api-conventions.md`](01-api-conventions.md) apply
throughout. Authenticated by session cookie (web UI) or API key (automation).

---

## 1. Endpoint index

| Group | Prefix | Module | Phase |
| --- | --- | --- | --- |
| Authentication | `/v1/auth`, `/v1/me` | M01 | 0 |
| Users | `/v1/users` | M01 | 1 |
| Grants | `/v1/grants` | M02 | 1 |
| API keys | `/v1/api-keys` | M02 | 1 |
| Knowledge bases | `/v1/knowledge-bases` | M03 | 2 |
| Documents | `/v1/documents`, `/v1/knowledge-bases/{id}/documents` | M03 | 2 |
| Chunks | `/v1/chunks` | M03 | 2 |
| Storage bindings | `/v1/storage-bindings` | M03 | 2 |
| Crawl jobs | `/v1/crawl-jobs` | M07 | 3 |
| Model providers | `/v1/model-providers`, `/v1/models` | M10 | 3 |
| Pipelines | `/v1/pipelines` | M11 | 4 |
| Functions | `/v1/functions` | M12 | 4 |
| Evaluation | `/v1/golden-sets`, `/v1/eval-runs` | M14 | 5 |
| Platform | `/v1/settings`, `/v1/quotas`, `/v1/audit-log`, `/v1/usage` | M15 | 1–3 |

---

## 2. Authentication (M01)

### `POST /v1/auth/login`

```jsonc
// request
{ "username": "admin", "password": "7Kq2-mVx9RtL4pZsN3wY" }
```

**Normal response — `200`**, plus `Set-Cookie: cairn_session=…; HttpOnly; Secure; SameSite=Lax`:

```jsonc
{ "status": "ok",
  "user": { "id":"usr_01HQ…", "username":"dana.ops", "role":"admin",
            "display_name":"Dana", "must_change_password": false } }
```

**Forced-change response — `200`, and deliberately NO session cookie** (`FR-A-04`):

```jsonc
{
  "status": "password_change_required",
  "reason": "initial_admin_setup",              // or "admin_forced" | "policy_expired"
  "change_token": "chg_01HQZX3N9K2M5P7R8T…",    // scope credential:bootstrap, TTL 600 s
  "expires_in": 600,
  "user": { "id": "usr_01HQ…", "username": "admin", "role": "admin" },
  "policy": {
    "min_length": 12,
    "require_classes": 3,
    "classes": ["lowercase", "uppercase", "digit", "symbol"],
    "username_editable": true,
    "disallow_previous": true
  }
}
```

The `change_token` cannot be used for anything except the endpoint below. Every other route
returns `403 PASSWORD_CHANGE_REQUIRED` (`FR-A-05`).

### `POST /v1/auth/complete-initial-setup`

`Authorization: Bearer <change_token>`

```jsonc
// request — username is optional (FR-A-07)
{ "current_password": "7Kq2-mVx9RtL4pZsN3wY",
  "new_username": "dana.ops",
  "new_password": "correct-horse-battery-staple-9",
  "confirm_password": "correct-horse-battery-staple-9" }
```

`200` with a session cookie set, or:

| Error | Meaning |
| --- | --- |
| `SETUP_TOKEN_INVALID` 401 | Missing, expired, wrong scope, or already used |
| `AUTHENTICATION_FAILED` 401 | `current_password` incorrect |
| `PASSWORD_POLICY_VIOLATION` 400 | `errors[]` lists each failed rule |
| `PASSWORD_REUSED` 400 | New equals current |
| `USERNAME_TAKEN` 409 | **Entire operation rolls back — the password is unchanged** |

### Other

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/v1/auth/logout` | Revokes the session |
| POST | `/v1/auth/change-password` | Voluntary change; bumps `credential_version` |
| GET | `/v1/me` | Current principal, effective permissions, `must_change_password` |

---

## 3. Users (M01, `FR-B-02`)

| Method | Path | Permission |
| --- | --- | --- |
| GET | `/v1/users` | `platform:users` |
| POST | `/v1/users` | `platform:users` |
| GET/PATCH/DELETE | `/v1/users/{id}` | `platform:users` |
| POST | `/v1/users/{id}/force-password-change` | `platform:users` |

```jsonc
// POST /v1/users
{ "username": "wei", "email": "wei@example.com", "display_name": "Wei",
  "role": "user", "send_invite": false }

// 201 — the initial password is returned ONCE; the account starts with
// must_change_password = true, so the identical forced flow applies (FR-A-12)
{ "user": { "id": "usr_02KM…", "username": "wei", "role": "user",
            "must_change_password": true },
  "initial_password": "Xy7-Kq2mVx9RtL4p" }
```

`DELETE` is a soft delete that revokes sessions, grants, and API keys. The last active admin
cannot be deleted or demoted (`LAST_ADMIN_PROTECTED`).

---

## 4. Grants and API keys (M02)

### `POST /v1/grants` (`FR-B-03`)

```jsonc
{ "subject_type": "user", "subject_id": "usr_02KM…",
  "resource_type": "knowledge_base", "resource_id": "kb_01HQ…",
  "permissions": ["kb:read", "kb:query", "kb:write"],
  "expires_at": null }
```

`GET /v1/users/{id}/permissions` returns the resolved effective set with the origin of each
permission — essential for answering "why can this user see that?".

### `POST /v1/api-keys` (`FR-B-08`, `FR-B-13`)

```jsonc
// request
{ "name": "support-agent-prod",
  "scopes": ["kb:query"],
  "knowledge_base_ids": ["kb_01HQ…"],
  "rate_limit_rpm": 600,
  "ip_allowlist": ["10.0.0.0/8"],
  "expires_at": "2027-08-28T00:00:00Z" }

// 201 — `key` is shown ONCE and is unrecoverable
{ "api_key": { "id": "key_01HQ…", "name": "support-agent-prod",
               "key_prefix": "cairn_sk_live_7Kq2", "last_four": "cA1e",
               "scopes": ["kb:query"], "knowledge_base_ids": ["kb_01HQ…"],
               "rate_limit_rpm": 600, "created_at": "2026-08-28T14:31:07Z" },
  "key": "cairn_sk_live_7Kq2mVx9RtL4pZsN3wYbG5hJ8dF6cA1e",
  "warning": "This key is displayed once and cannot be retrieved again." }
```

Requested scopes exceeding the owner's own permissions are rejected with `GRANT_EXCEEDS_OWNER`.

### `POST /v1/knowledge-bases/{id}/break-glass` (`FR-B-10`)

```jsonc
{ "reason": "Investigating retrieval quality incident INC-2026-0814 at the owner's request." }
// 201 → { "grant_id": "grant_01HQ…", "expires_at": "2026-08-28T15:31:07Z",
//         "notified": ["usr_02KM…"] }
```

Minimum 20-character reason; audited; owner notified.

---

## 5. Knowledge bases (M03)

### `POST /v1/knowledge-bases`

```jsonc
{
  "name": "Ops Handbook",
  "description": "Internal operations documentation and runbooks.",
  "embedding_model_id": "mdl_01HQ…",             // IMMUTABLE once indexed (FR-C-04)
  "metric": "cosine",
  "vector_binding_id": "bind_01HQ…",
  "object_binding_id": "bind_02KM…",
  "chunk_config": {
    "strategy": "parent_child",
    "child_tokens": 512, "child_overlap": 64, "parent_tokens": 2048,
    "keep_tables_intact": true, "min_chunk_tokens": 32
  },
  "retrieval_config": {
    "search_mode": "hybrid",
    "fusion": { "method": "rrf", "k": 60 },
    "top_k": 5, "candidate_k": 100, "score_threshold": 0.0,
    "rerank": { "enabled": true, "model_id": null, "top_n": 5 },
    "expand_parent": true
  },
  "metadata": { "team": "platform" }
}
```

### `PATCH /v1/knowledge-bases/{id}`

Attempting `embedding_model_id`, `embedding_dim`, or `metric` after indexing:

```jsonc
{ "status": 409, "code": "EMBEDDING_MODEL_IMMUTABLE",
  "detail": "The embedding configuration cannot be changed for an indexed knowledge base. Use POST /v1/knowledge-bases/{id}/reindex to rebuild with a different model.",
  "request_id": "req_…" }
```

Changing `chunk_config` succeeds but returns `"reindex_required": true` — the change does not
take effect until a rebuild.

### `POST /v1/knowledge-bases/{id}/reindex` (`FR-G-08`)

```jsonc
// request
{ "embedding_model_id": "mdl_03NP…",             // optional — the only way to change it
  "chunk_config": { "child_tokens": 384 },       // optional
  "confirm": true }

// 202
{ "index_version": 4, "estimated": { "chunks": 128440, "embedding_tokens": 41_200_000,
                                     "cost_usd": 4.12, "duration_minutes": 38 },
  "note": "The active index continues serving until the rebuild completes." }
```

Without `"confirm": true`, returns `200` with the estimate and does **not** start — the user must
see the cost before spending it.

### `GET /v1/knowledge-bases/{id}/index-progress` (`FR-G-09`)

```jsonc
{ "active_index_version": 3, "building_index_version": 4,
  "progress": { "chunks_total": 128440, "chunks_done": 71204, "percent": 55.4,
                "eta_seconds": 1020 },
  "state": "building", "started_at": "2026-08-28T14:02:00Z" }
```

---

## 6. Documents and chunks (M03)

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/v1/knowledge-bases/{id}/documents` | multipart; `metadata` as a JSON part (`FR-D-10`) |
| POST | `/v1/knowledge-bases/{id}/documents/bulk` | per-file results (`FR-D-02`) |
| GET | `/v1/knowledge-bases/{id}/documents` | filters: `state`, `source_type`, `q` |
| GET/DELETE | `/v1/documents/{id}` | |
| POST | `/v1/documents/{id}/retry` | resets attempts, re-enqueues |
| GET | `/v1/documents/{id}/chunks` | |
| PATCH | `/v1/chunks/{id}` | sets `is_edited` (`FR-F-09`) |
| POST | `/v1/chunks/{id}/split` · `/v1/chunks/merge` | |

Document list item — the shape that drives the most important UI screen:

```jsonc
{ "id": "doc_01HQ…", "title": "handbook-2026.pdf",
  "source_type": "upload", "mime_type": "application/pdf",
  "size_bytes": 4_812_004, "page_count": 312,
  "state": "failed", "stage_detail": "parse", "progress_pct": 0,
  "error_code": "PARSE_ENCRYPTED_PDF",
  "error_detail": "This PDF is password-protected. Remove the protection and re-upload.",
  "chunk_count": 0, "created_at": "2026-08-28T14:31:07Z", "indexed_at": null }
```

Bulk upload result, including the non-error skip case:

```jsonc
{ "results": [
    { "filename": "a.pdf", "status": "accepted", "document_id": "doc_01HQ…" },
    { "filename": "b.pdf", "status": "skipped", "reason": "DUPLICATE_CONTENT_HASH",
      "existing_document_id": "doc_02KM…" },
    { "filename": "c.exe", "status": "rejected", "code": "UNSUPPORTED_FILE_TYPE" } ],
  "accepted": 1, "skipped": 1, "rejected": 1 }
```

---

## 7. Crawl jobs (M07)

```jsonc
// POST /v1/crawl-jobs
{ "knowledge_base_id": "kb_01HQ…", "name": "Product docs",
  "seeds": ["https://docs.example.com/"],
  "config": { "max_depth": 3, "max_pages": 1000,
              "include_patterns": ["^https://docs\\.example\\.com/guide/"],
              "exclude_patterns": ["/changelog/"],
              "same_domain_only": true, "respect_robots": true,
              "render_js": true, "delay_ms": 500, "concurrency": 4,
              "use_sitemap": true, "remove_missing": false },
  "schedule_cron": "0 2 * * *" }
```

Run report (`FR-E-11`):

```jsonc
{ "run_id": "run_01HQ…", "state": "completed",
  "stats": { "fetched": 412, "added": 380, "updated": 24, "unchanged": 8,
             "skipped_robots": 8, "skipped_scope": 141, "failed": 0, "gone": 2 },
  "started_at": "2026-08-28T02:00:00Z", "finished_at": "2026-08-28T02:07:41Z" }
```

---

## 8. Models (M10)

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/v1/model-providers` | Credentials write-only; never returned |
| GET | `/v1/model-providers/{id}/discover` | Lists models the provider exposes |
| POST | `/v1/models` | Register with capability and dimension |
| POST | `/v1/models/{id}/test` | (`FR-K-05`) |
| POST | `/v1/models/{id}/chat` | SSE test console (`FR-N-09`) |

```jsonc
// POST /v1/models/{id}/test → 200
{ "reachable": true, "latency_ms": 187,
  "observed_dimension": 1024, "configured_dimension": 1024, "match": true }
```

The dimension check catches a misconfiguration at registration rather than as a mysterious
failure 40,000 documents into an ingest.

---

## 9. Platform (M15)

| Method | Path | Permission |
| --- | --- | --- |
| GET | `/v1/audit-log` | `platform:audit` — filters: `actor_id`, `action`, `resource_type`, `resource_id`, `from`, `to`, `outcome` |
| GET | `/v1/audit-log/export?format=jsonl` | `platform:audit` — streamed |
| GET/PATCH | `/v1/settings` | `platform:settings` |
| GET/PATCH | `/v1/quotas` | `platform:settings` |
| GET | `/v1/usage?group_by=kb&period=30d` | `platform:audit` or self-scoped |

```jsonc
// GET /v1/audit-log item
{ "id": 84213, "at": "2026-08-28T14:31:07Z",
  "actor_type": "user", "actor_id": "usr_01HQ…", "actor_label": "dana.ops",
  "action": "break_glass.open", "outcome": "success",
  "resource_type": "knowledge_base", "resource_id": "kb_01HQ…",
  "ip": "10.4.2.19", "request_id": "req_01HQ…",
  "detail": { "reason": "Investigating INC-2026-0814",
              "expires_at": "2026-08-28T15:31:07Z" } }
```

---

## 10. Meta

`GET /v1/meta` — unauthenticated, for clients to discover capability:

```jsonc
{ "product": "Cairn", "version": "0.4.2", "api_version": "v1",
  "capabilities": { "mcp": true, "dify_compat": true, "hybrid_search": true,
                    "rerank": true, "pipelines": false, "functions": false },
  "limits": { "max_top_k": 100, "max_targets": 10, "max_batch": 20,
              "max_upload_bytes": 209715200 },
  "docs_url": "https://docs.cairn.io" }
```

`capabilities` reflects what this deployment actually has enabled, so a client can adapt rather
than discovering a missing feature via a 404.
