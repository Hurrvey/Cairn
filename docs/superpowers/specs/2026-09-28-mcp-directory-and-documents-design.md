# MCP `list_knowledge_bases`, `get_document` and resources (T-M13-03/05/06/10) — Design

## September 29 review resolution (supersedes conflicting proposals below)

Approved by the persistent design advisor `/root/design_advisor`:

- Return ordered indexed passages, with chunk IDs/ordinals and an explicit overlap note. Do not reconstruct original text or use parent snapshots. Require schema 2 and valid ordinals, identical document revision and point identities.
- Scroll the fixed ACTIVE namespace with a document filter, payload-only and a field whitelist; UUID cursor uses Qdrant's inclusive next-page-offset convention. Page size 64, maximum 256; hard limits 10,000 points, 8 MiB selected payload, 10 seconds. Collect before ordinal sort; never mistake the first UUID page for the beginning of the document.
- Character limit defaults to 50,000 (max 200,000), with an additional UTF-8 serialized tool-result limit of 512 KiB. Return truncation explicitly. Text content summarizes; structured content carries passages.
- Directory is bounded to 500 authorized entries; tool description to 100 entries/8 KiB. No process-local permission cache. Distinguish missing runtime from Redis failure. Structured content is an object.
- Require kb:read for get_document/resources, kb:query for search/directory. Check authorization before runtime/storage. Resource reads reuse the same service and fail through protocol errors.
- Add directory fields to runtime projection and ordinal to normal/reembed payloads. Republish after ACTIVE indexing and document purge; counts derive from catalog rows.

Implementation order: scroll contract tests/driver; read service tests/implementation; projection and ingestion facts; tool/resource transport and real SDK acceptance. Preserve data-plane import contracts.

**Date:** 2026-09-28 · **Status:** Draft for independent review · **Spec:** M13
(`docs/02-modules/M13-mcp-server.md`). Today MCP exposes only `search_knowledge_base`, with a
generic description and an ID-only enum.

## Constraint

MCP is data plane (ADR-0002): it may use Redis, the vector store and `cairn.authz.dataplane`, never
catalog/identity/PostgreSQL or object storage (import contract `dataplane`). Knowledge base names
and document text therefore have to reach it through the data plane's own sources.

## 1. Directory fields in the runtime projection

`KnowledgeBaseRuntime` (Redis `kb:runtime:{id}`, published by the catalog) gains a `directory`
block, default empty so older projections parse:

```
directory: {name, description, doc_count, chunk_count, last_indexed_at}
```

- Filled by `CatalogService.publish_runtime` from the `knowledge_base` row.
- Counts change as documents are indexed or deleted. `publish_runtime` is already called on
  activation, config change, rebuild and deletion; it is additionally called after **every**
  committed index of a document into the ACTIVE version and after a document purge (one Redis SET;
  ingestion rates are far below Redis limits). Counts are therefore current to within one document
  commit; they are presented as "about".
- Name/description edits (`PATCH /v1/knowledge-bases/{id}`) republish (verify; add if missing).

## 2. Tools

- **`list_knowledge_bases`** (no arguments): the principal's `kb:query`-able KBs whose runtime is
  published, each `{id, name, description, doc_count, chunk_count, last_indexed_at,
  permissions}`; text content is a readable list; `structuredContent` the list. KBs without a
  published runtime are listed with `status: "not_ready"` rather than hidden, so a user can see why
  search fails. Bounded to 500 entries.
- **`search_knowledge_base`**: description generated per principal from the same directory
  (`- kb_… — Name: description (N documents, updated YYYY-MM-DD)`), bounded to 100 entries and 8 KB
  of description; cached per `(principal id, perm_version)` for 60 s in process (TC-M13-15: the
  perm_version is in the key). Optional `filters` argument (document metadata, as the HTTP API;
  same bounds) and `document_ids`.
- **`get_document`** `{document_id, knowledge_base_id?, max_characters (default 50 000, max
  200 000)}` — requires **`kb:read`** (FR-B-07; TC-M13-07). Implemented from the read model: scroll
  the ACTIVE namespace with filter `document_id == id`, order by chunk `ordinal`, and rebuild the
  text:
  - chunks with a parent snapshot contribute the parent text once per parent (parent-child chunking
    covers the document without overlap);
  - otherwise each chunk's content in order; configured chunk overlap is removed by trimming the
    longest suffix/prefix overlap (bounded to the configured overlap) between neighbours.
  The result states it is the indexed text of revision N, with title/type from `doc_*`, and is
  truncated at `max_characters` with a note. A document the key cannot read → the same
  `PERMISSION_DENIED`/not-found tool error as an unknown one (no existence oracle).
  `knowledge_base_id` is required when the key can read more than one KB (document IDs do not
  reveal their KB on the data plane).

## 3. Read model and store additions

- Point payload v2 gains `ordinal` (chunk ordinal) — already listed in ADR-0005's payload, never
  written. Versions built before it lack it; `get_document` on them orders by `(parent snapshot
  ordinal, chunk_id)` fallback and says the order may be approximate. (Payload schema stays 2: no
  v2 index has been released yet; this lands before release.)
- `VectorStore.scroll(ns, filter, *, limit, cursor) -> (hits, next_cursor)` — payload-only
  paging by filter (Qdrant `scroll`, fake: sorted by id). Conformance tests for both drivers. Used
  only by `get_document`; bounded to 10 000 points per document (larger documents answer
  "too large for MCP, use the HTTP content endpoint").

## 4. Resources (FR-J-07, P2)

`resources/list` returns `cairn://kb/{kb_id}` for each readable KB and `resources/templates/list`
advertises `cairn://kb/{kb_id}/documents/{doc_id}`; `resources/read` of a document URI returns the
same text as `get_document` (`kb:read` required). No document enumeration on the data plane (the
read model has no document list without scanning); clients get document IDs from search results.

## 5. Verification

TC-M13-02/03/04/07/09/15 as unit tests with the fake store and a fake runtime loader;
`get_document` reconstruction (parent-child, flat with overlap, truncation, permission); scroll
conformance on fake + Qdrant; integration over real Streamable HTTP with the official MCP SDK
client (existing harness in `tests/integration/test_mcp_acceptance.py`): list → search with filter
→ get_document with a `kb:read` key and a refusal with a `kb:query` key; import-linter unchanged.
