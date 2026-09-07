# Glossary — Ubiquitous Language

**Document:** `00-overview/02-glossary.md`
**Status:** Normative
**Date:** 2026-08-28

These terms are **normative**. Use them exactly — in code identifiers, table names, API
fields, log messages, and UI copy. A synonym in code is a bug: it makes the system
un-searchable and un-navigable for both humans and Agents.

---

## Core domain

| Term | Definition | Code identifier |
| --- | --- | --- |
| **Workspace** | The tenancy boundary. Every resource belongs to exactly one workspace. Single-tenant deployments have exactly one. | `workspace`, `workspace_id` |
| **Knowledge Base** (KB) | A named collection of documents sharing one embedding model, one chunking configuration, and one retrieval configuration. The unit of retrieval targeting and permission granting. | `knowledge_base`, `kb_id` |
| **Document** | One source artifact registered in a KB — an uploaded file, a crawled page, or a connector record. Has a lifecycle state. | `document`, `document_id` |
| **Revision** | A version of a document's content, identified by `content_hash`. Re-ingesting identical content is a no-op. | `document.revision` |
| **Chunk** | A retrievable unit of text derived from a document. The thing that gets embedded and returned. | `chunk`, `chunk_id` |
| **Parent Chunk** | A larger enclosing window returned in place of a matched child chunk when `expand_parent` is on. Embed small, return big. | `chunk.parent_id` |
| **Index Version** | A monotonically increasing integer per KB identifying one complete build of its vector index. Enables blue/green reindex. | `index_version` |
| **Namespace** | The addressing tuple `(kb_id, index_version)` used to locate vectors. Hides the physical collection layout. | `Namespace` |

## Ingestion

| Term | Definition | Code identifier |
| --- | --- | --- |
| **Source** | Where a document came from: `upload`, `crawl`, `s3_sync`, `connector`. | `document.source_type` |
| **Ingestion** | The whole process of turning a source artifact into indexed chunks. | `ingestion` |
| **Stage** | One step of ingestion: `fetch`, `parse`, `chunk`, `enrich`, `embed`, `index`. | `stage` |
| **Parser** | Converts raw bytes of one MIME type into normalized Markdown plus layout metadata. | `Parser` |
| **Chunker** | Splits a parsed document into chunks according to a strategy. | `Chunker` |
| **Enricher** | Adds metadata to a chunk — summary, keywords, entities, classification. | `Enricher` |
| **Crawl Job** | A scheduled or one-off web crawl definition with scope rules. | `crawl_job` |
| **Content Hash** | SHA-256 of normalized content, used for deduplication and idempotency at document and chunk level. | `content_hash` |

## Retrieval

| Term | Definition | Code identifier |
| --- | --- | --- |
| **Query** | The caller's natural-language search string. | `query` |
| **Target** | One `(knowledge_base_id, weight)` pair in a retrieval request. A request may have several. | `RetrievalTarget` |
| **Dense search** | Vector similarity search over embeddings. | `search_mode="vector"` |
| **Sparse search** | Lexical/BM25 search over terms. | `search_mode="fulltext"` |
| **Hybrid search** | Dense + sparse run concurrently and fused. | `search_mode="hybrid"` |
| **Fusion** | Combining ranked lists from multiple retrievers. Default: Reciprocal Rank Fusion. | `fusion`, `rrf` |
| **Rerank** | Cross-encoder re-scoring of a candidate set. Expensive, high precision. | `rerank` |
| **Hit** | One retrieved chunk with its scores, metadata, and source. | `Hit` |
| **Candidate set** | The pre-rerank result list, typically top-100. | `candidates` |
| **Token budget** | A caller-specified maximum total tokens for returned content; Cairn trims to it. | `max_context_tokens` |
| **Explain** | Debug mode returning per-stage candidate lists and scores. | `explain` |

## Access control

| Term | Definition | Code identifier |
| --- | --- | --- |
| **Principal** | An authenticated actor: a `User` or an `ApiKey`. Both can hold grants. | `Principal` |
| **Role** | Platform-level classification: `admin` or `user`. Governs platform capabilities only. | `user.role` |
| **Grant** | A row assigning a permission set on one resource to one subject. | `resource_grant` |
| **Permission** | A verb on a resource type, e.g. `kb:query`. | `Permission` |
| **Effective permissions** | The resolved union of a principal's grants; for API keys, intersected with the owner's. | `EffectivePermissions` |
| **Break-glass** | An audited, time-boxed self-grant allowing an admin to read tenant content. | `break_glass_grant` |
| **Forced credential change** | The state in which a principal may only change its own credentials, enforced server-side. | `must_change_password` |

## Platform

| Term | Definition | Code identifier |
| --- | --- | --- |
| **Control plane** | Management surface: CRUD, config, jobs, admin. Latency-tolerant. | `CAIRN_ROLE=control` |
| **Data plane** | Retrieval surface. Latency-critical, SLO-bound, read-only. | `CAIRN_ROLE=data` |
| **Task** | One durable unit of background work, stored in PostgreSQL. | `task` |
| **Queue** | A named class of tasks sharing a resource profile: `fetch`, `parse`, `ocr`, `embed`, `index`, `maintain`. | `task.queue` |
| **Lease** | A time-bounded claim on a task by a worker; expires and is reclaimed if not heartbeated. | `lease_until` |
| **Storage Binding** | A configured, credentialed connection to a vector store or object store. | `storage_binding` |
| **Model Provider** | A configured LLM/embedding/rerank endpoint (OpenAI, Anthropic, Ollama, …). | `model_provider` |
| **Pipeline** | A versioned DAG applied to ingestion or retrieval. The reframed "Agent Workflow". | `pipeline` |
| **Function** | A sandboxed user-supplied transform conforming to a typed slot signature. | `function` |
| **Golden Set** | A labelled `(query, relevant_chunk_ids)` dataset used to measure retrieval quality. | `golden_set` |

---

## Deliberately avoided terms

Using these creates ambiguity. Use the replacement.

| Do not use | Use instead | Why |
| --- | --- | --- |
| "Dataset" | **Knowledge Base** | Collides with evaluation datasets |
| "Collection" | **Namespace** (logical) / "physical collection" (Qdrant only) | Layout is an implementation detail |
| "Segment" | **Chunk** | "Segment" is a Qdrant storage term |
| "Job" alone | **Task** (unit of work) or **Crawl Job** (crawl definition) | Two different things |
| "Tenant" | **Workspace** | One canonical word |
| "Agent" for our own workers | **Worker** | "Agent" means an external client of Cairn |
| "Embedding" as a verb-noun for the process | **Vectorization** (process), **embedding** (the vector) | Precision |
| "Search" and "retrieval" interchangeably | **Search** = one retriever's operation; **Retrieval** = the whole pipeline | They are different scopes |
| "Group" | **Workspace** or **Role** | Ambiguous |
| "Permission" for a role | Roles are `admin`/`user`; permissions are `kb:query` etc. | Distinct concepts |

---

## Naming conventions derived from this glossary

- Database tables: singular snake_case — `knowledge_base`, not `knowledge_bases`.
- Primary keys: `id`. Foreign keys: `<entity>_id` — `kb_id`, `document_id`.
- API JSON fields: snake_case — `knowledge_base_id`, `top_k`.
- Python classes: PascalCase of the term — `KnowledgeBase`, `RetrievalTarget`.
- Public IDs are prefixed ULIDs — `kb_01H…`, `doc_01H…`, `chk_01H…`. See
  [`../01-architecture/06-cross-cutting-conventions.md`](../01-architecture/06-cross-cutting-conventions.md) §2.
- Permission strings: `<resource>:<verb>`, lowercase — `kb:query`, `pipeline:run`.
- Metric names: `cairn_<subsystem>_<measure>_<unit>` — `cairn_retrieval_latency_seconds`.
