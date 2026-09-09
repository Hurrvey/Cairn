# M07 — Ingestion (Sources, Parsers, Chunkers, Enrichers)

| | |
| --- | --- |
| **Package** | `cairn.ingestion` |
| **Layer** | L3 worker |
| **Phase** | 2 (upload, parse, chunk, index) · 3 (crawl, OCR, archives) |
| **Owner** | Backend eng. C |
| **Depends on** | M00, M02 (facade), M03 (facade), M04, M05, M06, M08, M10 (facade), M12 (facade) |
| **Depended on by** | apps/worker |
| **Tables owned** | `crawl_job`, `crawl_run`, `crawl_url` |
| **Requirements owned** | FR-D-03,04,07,08,10; FR-E-01..11; FR-F-01..08,10,11; FR-G-06; NFR-P-04; NFR-SEC-08,09 |

---

## 1. Purpose and scope

Turning source artifacts into indexed chunks. This is where **retrieval quality is won or
lost** — far more than in the vector database.

**In scope:** the stage pipeline, source adapters (upload, crawl), parsers, chunkers, enrichers,
the index writer, incremental reprocessing, crawl scheduling and scope control.

**Out of scope:** registering documents (M03), generating embeddings (M08), search (M09),
executing user pipelines (M11 — this module provides the *default* pipeline).

---

## 2. The stage pipeline

```
registered → [fetch] → fetched → [parse] → parsed → [chunk] → chunked
           → [embed] → embedded → [index] → indexed
                                            ↘ failed(stage, code)
                                            ↘ skipped(DUPLICATE_CONTENT_HASH)
```

Each stage:
- is a separate task on its own queue (`M06`),
- commits its state transition and the next task **in one transaction** (`NFR-R-03`),
- is **idempotent** — rerunning produces the same result (`NFR-R-06`),
- is **resumable** — a crash resumes at the last committed stage, never from the start
  (`NFR-R-05`).

```python
async def handle_parse(ctx: TaskContext) -> TaskResult:
    doc = await catalog.get_document(ctx.document_id)
    if doc.state in TERMINAL_STATES:
        return TaskResult.skipped("already processed")      # idempotency guard

    raw = await objectstore.get(doc.object_key)
    parsed = await parser_registry.parse(raw, mime=doc.mime_type, ctx=ParseContext(doc))
    key = f"{doc.workspace_id}/{doc.kb_id}/parsed/{doc.id}/{doc.revision}/content.md"
    await objectstore.put(key, parsed.markdown)

    async with transaction() as session:
        await catalog.update_document_state(session, doc.id, "parsed",
                                            parsed_object_key=key, page_count=parsed.pages)
        await tasks.enqueue(session, TaskSpec(queue="chunk", kind="document.chunk",
                                              document_id=doc.id,
                                              dedupe_key=f"chunk:{doc.id}:{doc.revision}"))
    return TaskResult.ok()
```

---

## 3. Public interface

```python
class IngestionService:
    async def reprocess_document(self, doc_id: UUID, from_stage: Stage) -> None: ...
    async def reindex_kb(self, kb_id: UUID, index_version: int) -> None: ...

class CrawlService:
    async def create_job(self, actor, kb_id, spec: CrawlJobSpec) -> CrawlJobView: ...
    async def run_now(self, actor, job_id: UUID) -> CrawlRunView: ...
    async def cancel_run(self, actor, run_id: UUID) -> None: ...
    async def list_runs(self, job_id, page) -> CursorPage[CrawlRunView]: ...
    async def get_run_report(self, run_id: UUID) -> CrawlReport: ...

# Extension points — also the Function slot signatures (FR-M-02)
class Parser(Protocol):
    supported_mimes: ClassVar[frozenset[str]]
    async def parse(self, data: bytes, ctx: ParseContext) -> ParsedDocument: ...

class Chunker(Protocol):
    async def chunk(self, doc: ParsedDocument, cfg: ChunkConfig) -> list[ChunkSpec]: ...

class Enricher(Protocol):
    async def enrich(self, chunk: ChunkSpec, ctx: EnrichContext) -> ChunkSpec: ...
```

```python
@dataclass
class ParsedDocument:
    markdown: str
    blocks: list[Block]          # typed: heading | paragraph | table | list | code | image
    pages: int
    language: str                # detected; drives tokenizer and analyzer selection
    metadata: dict[str, Any]
    assets: list[Asset]          # extracted images, stored separately
```

---

## 4. Parsing (`FR-F-01..04`)

### 4.1 Registry

| MIME | Parser | Notes |
| --- | --- | --- |
| `application/pdf` | `PdfParser` | Layout engine — see §4.2 |
| `…wordprocessingml…` | `DocxParser` | python-docx; heading levels preserved |
| `…presentationml…` | `PptxParser` | One block per slide |
| `…spreadsheetml…`, `text/csv` | `SheetParser` | Each sheet a table block; header row retained per chunk |
| `text/html` | `HtmlParser` | trafilatura main-content extraction |
| `text/markdown`, `text/plain` | `MarkdownParser` | Passthrough with heading structure |
| `application/epub+zip` | `EpubParser` | Chapter blocks |
| `image/*` | `ImageParser` | OCR |
| `application/json` | `JsonParser` | Configurable field mapping |
| `application/zip` | `ArchiveParser` | Expands into child documents (`FR-D-04`) |

### 4.2 PDF — the highest-leverage decision in the system

Naive text extraction on a two-column PDF interleaves the columns; a financial table becomes an
unreadable word sequence. **No amount of retrieval tuning recovers from this** — the
information was destroyed at ingest. Teams routinely spend months tuning HNSW parameters while
their content was scrambled from day one.

**Spike `T-M07-01` (Phase 2, 3 days, blocking)** evaluates on a fixed 30-document corpus
(academic PDFs, financial reports with tables, scanned documents, CJK documents, multi-column
layouts):

| Candidate | Licence | Evaluate on |
| --- | --- | --- |
| **Docling** (IBM) | **MIT** ✅ | Table fidelity, reading order, speed |
| pypdfium2 + custom layout | Apache/BSD ✅ | Baseline speed, quality floor |
| MinerU | Version-dependent; current upstream has commercial-restricted additional terms ⚠️ | Quality ceiling; explicit licence review required before distribution |
| PyMuPDF | AGPL/commercial ⚠️ | Speed; requires a licence decision (`RISK-11`) |

Scoring: table-structure F1, reading-order correctness, pages/second, licence risk.
**Docling is the presumed default** precisely because it is MIT — see
[technology stack §7](../01-architecture/02-technology-stack.md).

The [2026-09-07 checkpoint](../04-plan/05-parser-spike.md) pins the inspected
licence sources and records the still-open 30-document quality gate. A permissive
top-level licence does not establish the licences of model weights or dependencies.

Output must be normalized Markdown with tables as GFM tables, plus a `layout.json` carrying
per-block bounding boxes and page numbers for citation (`FR-F-07`).

### 4.3 OCR (`FR-F-03`)

Auto-detect: if extractable text density < 100 chars/page, treat as scanned and route to the
`ocr` queue. RapidOCR by default; PaddleOCR for CJK-heavy workloads. Per-page processing with
heartbeats. Results are cached by page image hash so a retry does not redo completed pages.

---

## 5. Chunking (`FR-F-05..07`)

Implementation checkpoint (2026-09-07): `DocumentChunker` implements `fixed`,
`recursive`, `markdown` and `parent_child`. Construct it with `document_id`, an
explicitly loaded model `tokenizer`, and `index_version`, then call `chunk(doc, cfg)`
for catalog `ChunkSpec` values. `semantic` and `custom` remain unsupported.
See [rules, identity and validation](../04-plan/08-chunking-implementation.md).
This is not worker pipeline wiring. Parents carry `metadata.embed=false`; a
future embedding handler must honor that marker. Citation metadata retains all
contributing block ranges; oversized protected tables are explicitly flagged.

Continuation (2026-09-08): see the [execution ledger](../04-plan/15-execution-ledger.md) for the
current accepted scope. Semantic and custom library strategies now have validated implementations;
the chunk worker composes semantic with the selected embedding service and accepts custom only through
an explicit scoped resolver. Parse/chunk/embed/index normal paths, failure fencing and incremental
source replacement have real-service tests. Full-KB fan-out/re-embedding lifecycle, PDF/OCR quality
and production M12 sandbox execution remain open; the overall knowledge-base E2E gate is not closed.

| Strategy | Description | Use for |
| --- | --- | --- |
| `fixed` | N tokens, M overlap | Baseline, uniform text |
| `recursive` | Split on a separator hierarchy, largest first | General prose |
| `markdown` | Heading-aware; never splits across `##` | Documentation |
| `semantic` | Boundaries at embedding-distance peaks | Unstructured narrative |
| `parent_child` **(default)** | Embed small, return large | **Best general default** |
| `custom` | A user Function (`FR-M-02`) | Domain-specific |

### Parent–child (`FR-F-06`) — the default, and usually the largest single quality win

```
Parent window: 2048 tokens  ← returned to the caller (full context)
  ├── Child 512 tokens      ← embedded and searched (precise match)
  ├── Child 512 tokens
  └── Child 512 tokens
```

Small chunks match precisely because they are not diluted by surrounding text; large returns
give the LLM the context it needs to actually answer. Retrieval returns the parent when
`expand_parent` is on (`FR-H-10`).

Children carry `parent_id`; parents are stored in `chunk` with `parent_id IS NULL` and are
**not embedded** — they exist only to be returned.

### Rules that apply to every strategy

1. **Tables are never split** when `keep_tables_intact` is set. A table split mid-row is
   useless to a model. Oversized tables become their own chunk regardless of the token target.
2. **Header context is prepended.** Each chunk begins with its `heading_path`
   (`# Security > ## Key Management`), so a chunk retrieved in isolation still says what it is
   about. Cheap, and a consistent measurable quality gain.
3. **Chunks below `min_chunk_tokens` merge forward** — a 12-token orphan is noise in the index.
4. **CJK has no whitespace word boundaries.** Sentence splitting uses `。！？；` alongside
   `.!?;`, and token counting uses the embedding model's own tokenizer (`FR-F-11`), never
   `len(text.split())`.
5. Every chunk carries `heading_path`, `page`, `source_url`, `ordinal` (`FR-F-07`).

---

## 6. Incremental reprocessing (`FR-G-06`, `FR-D-08`)

```python
async def handle_chunk(ctx) -> TaskResult:
    new_chunks = await chunker.chunk(parsed, kb.chunk_config)
    existing   = await catalog.get_chunk_hashes(doc.id, index_version)

    new_hashes = {c.content_hash for c in new_chunks}
    to_embed   = [c for c in new_chunks if c.content_hash not in existing]
    to_delete  = existing - new_hashes

    await catalog.replace_chunks(doc.id, index_version, new_chunks)
    if to_delete:
        await vectorstore.delete(ns, ids=to_delete)
    # only genuinely new content is embedded
    await tasks.enqueue_many(session, [embed_task(c) for c in to_embed])
```

On a 10k-document KB where 200 documents changed, this is a 20-minute job instead of a 6-hour
one. Combined with the embedding cache (`FR-G-02`), a chunking-only reindex of unchanged content
makes almost no provider calls.

---

## 7. Web crawling (`FR-E-01..11`)

### Configuration

```jsonc
{
  "seeds": ["https://docs.example.com/"],
  "max_depth": 3, "max_pages": 1000,
  "include_patterns": ["^https://docs\\.example\\.com/guide/"],
  "exclude_patterns": ["/changelog/", "\\.pdf$"],
  "same_domain_only": true,
  "respect_robots": true,
  "render_js": true,
  "delay_ms": 500, "concurrency": 4,
  "use_sitemap": true,
  "remove_missing": false,
  "user_agent": "Cairn-Crawler/1.0 (+https://…)"
}
```

### Algorithm

```
1. Load robots.txt per domain (cached 1 h); discover sitemaps if enabled
2. Frontier = seeds ∪ sitemap URLs, deduplicated by normalized URL hash
3. For each URL, in scope, under depth and page caps:
     a. SSRF guard — core.http.safe_client()          ← NFR-SEC-09, non-negotiable
     b. Conditional GET with stored ETag / If-Modified-Since
     c. 304 or identical content_hash → mark unchanged, DO NOT reparse   ← FR-E-07
     d. Render with Playwright when render_js
     e. trafilatura main-content extraction            ← FR-E-05
     f. Register or update the document via M03
     g. Extract links → frontier at depth+1
4. Per-domain politeness: delay, concurrency cap, honour Retry-After   ← FR-E-09
5. After the run: URLs in crawl_url not seen this run → status 'gone';
   if remove_missing, delete their documents            ← FR-E-08
6. Write the run report                                 ← FR-E-11
```

**SSRF is the sharp edge here.** A crawl seed is attacker-controlled input by definition. An
attacker adds `http://169.254.169.254/latest/meta-data/iam/security-credentials/` as a seed and
reads the deployment's cloud credentials. Every fetch goes through `core.http.safe_client()`,
which resolves DNS itself, validates the resolved IP, pins the connection to it, and revalidates
on every redirect hop. Crawl workers should additionally run in a network segment with no route
to internal services — defence in depth, because one missed code path should not be fatal.

### Scheduling (`FR-E-06`)

`crawl_job.schedule_cron` is evaluated by `worker-maintain` each minute; due jobs enqueue a
`fetch` task. Concurrent runs of the same job are prevented by `dedupe_key = f"crawl:{job_id}"`.

---

## 8. Archive safety (`FR-D-04`, `NFR-SEC-08`)

| Threat | Control |
| --- | --- |
| Zip-slip path traversal | Reject any entry whose resolved path escapes the extraction root |
| Decompression bomb | Abort if total uncompressed > 10× compressed **or** > 2 GB |
| Entry-count explosion | Cap at 10,000 entries |
| Nested archives | Depth limited to 2 |
| Symlink entries | Rejected outright |

Extraction is streamed to a temporary directory, never into memory.

---

## 9. Enrichment (`FR-F-10`) — Phase 4, optional per KB

`summary`, `keywords`, `questions` (hypothetical questions embedded alongside content, which
markedly improves question-style query matching), `classification`, `entities`.

Enrichment costs an LLM call per chunk. It is **off by default**, cost is estimated and shown
before enabling, and it runs on the `maintain` queue so it never delays primary indexing.

---

## 10. Performance requirements

| Metric | Target | Req |
| --- | --- | --- |
| Parse throughput | ≥ 120 docs/hour/core, typical 100-page PDF | NFR-P-04 |
| Parse latency | < 30 s p95 for a 100-page PDF | |
| OCR | < 3 s/page | |
| Chunking | < 2 s for a 300-chunk document | |
| Crawl | ≥ 10 pages/s (no JS), ≥ 2 pages/s (JS) | |
| Index write | ≥ 5000 chunks/s | NFR-P-08 |
| Memory per parse worker | < 2 GB RSS steady state | |

**Parse dominates by an order of magnitude** — it is the scaling unit. 100k documents on 8
cores ≈ 70 hours; on 64 cores ≈ 9 hours.

---

## 11. Error codes

| Code | Stage | Retryable | User message |
| --- | --- | --- | --- |
| `PARSE_ENCRYPTED_PDF` | parse | ❌ | "This PDF is password-protected. Remove the protection and re-upload." |
| `PARSE_CORRUPT_FILE` | parse | ❌ | "The file could not be read. It may be corrupt." |
| `PARSE_UNSUPPORTED_MIME` | parse | ❌ | "This file type is not supported." |
| `PARSE_TIMEOUT` | parse | ✅ | "Processing took too long and will be retried." |
| `PARSE_EMPTY_CONTENT` | parse | ❌ | "No extractable text was found. If this is a scan, enable OCR." |
| `PARSE_TOO_LARGE` | parse | ❌ | "The file exceeds the configured parsing size limit." |
| `OCR_FAILED` | ocr | ✅ | |
| `CHUNK_CONFIG_INVALID` | chunk | ❌ | "The chunking configuration is invalid." |
| `CHUNK_UNSUPPORTED_STRATEGY` | chunk | ❌ | "The configured chunking strategy is not available." |
| `CHUNK_BUDGET_EXCEEDED` | chunk | ❌ | "The chunk token budget cannot accommodate the heading and source text." |
| `EMBED_PROVIDER_ERROR` | embed | ✅ | "The embedding provider is unavailable." |
| `EMBED_QUOTA_EXCEEDED` | embed | ❌ | "The embedding token quota has been exhausted." |
| `INDEX_DIMENSION_MISMATCH` | index | ❌ | Internal — indicates a bug; alert. |
| `CRAWL_ROBOTS_DISALLOWED` | fetch | ❌ | "robots.txt disallows this URL." |
| `CRAWL_BLOCKED_ADDRESS` | fetch | ❌ | "This address is not permitted." |
| `CRAWL_HTTP_ERROR` | fetch | ✅ | |
| `ARCHIVE_UNSAFE` | parse | ❌ | "This archive failed safety checks." |

> Every message is written for a **content contributor**, not an engineer — this table is the
> product surface for `FR-P-05`, the most-complained-about area of every RAG product.

---

## 12. Test requirements

| ID | Test | Req |
| --- | --- | --- |
| TC-M07-01 | Each parser produces normalized Markdown with structure preserved | FR-F-01 |
| TC-M07-02 | **Two-column PDF yields correct reading order** | FR-F-02 |
| TC-M07-03 | **Table-bearing PDF preserves table structure as GFM** | FR-F-02 |
| TC-M07-04 | Scanned PDF auto-routes to OCR | FR-F-03 |
| TC-M07-05 | Encrypted PDF fails terminally with the right code, no retry | — |
| TC-M07-06 | CJK document parses, chunks, and counts tokens correctly | FR-F-04 |
| TC-M07-07 | `markdown` chunker never splits across `##` | FR-F-05 |
| TC-M07-08 | `parent_child` produces correct parent/child links | FR-F-06 |
| TC-M07-09 | Tables are not split when `keep_tables_intact` | §5 rule 1 |
| TC-M07-10 | Heading path is prepended to every chunk | §5 rule 2 |
| TC-M07-11 | Sub-minimum chunks merge forward | §5 rule 3 |
| TC-M07-12 | Every chunk carries page and heading provenance | FR-F-07 |
| TC-M07-13 | **Re-ingesting identical content re-embeds nothing** | FR-G-06 |
| TC-M07-14 | Changing one paragraph re-embeds only the affected chunks | FR-G-06 |
| TC-M07-15 | **Crash at each stage resumes from that stage, not the start** | NFR-R-05 |
| TC-M07-16 | Every stage handler is idempotent under double execution | NFR-R-06 |
| TC-M07-17 | Crawl respects include/exclude and depth | FR-E-02 |
| TC-M07-18 | Crawl respects robots.txt and reports skips | FR-E-03 |
| TC-M07-19 | **Crawl of a private/metadata address is blocked** | NFR-SEC-09 |
| TC-M07-20 | **Redirect to a private address is blocked on the redirect hop** | NFR-SEC-09 |
| TC-M07-21 | Recrawl of unchanged pages does not reparse | FR-E-07 |
| TC-M07-22 | Removed pages are detected and optionally deleted | FR-E-08 |
| TC-M07-23 | Per-domain delay and concurrency are honoured | FR-E-09 |
| TC-M07-24 | Zip-slip archive is rejected | NFR-SEC-08 |
| TC-M07-25 | Decompression bomb is rejected before exhausting disk | NFR-SEC-08 |
| TC-M07-26 | Benchmark: ≥ 120 docs/hour/core | NFR-P-04 |
| TC-M07-27 | Parse worker RSS stays under 2 GB over 500 documents | §10 |

Coverage target: **80%** (parsers are I/O heavy; the reference corpus carries the weight).

---

## 13. Acceptance criteria

- [ ] Spike `T-M07-01` completed with a written recommendation and a licence decision
- [ ] Reference corpus of 30 documents committed with expected-output snapshots
- [ ] Journey J3 passes end to end including failure and duplicate paths
- [ ] Journey J6 passes including incremental recrawl
- [ ] SSRF tests pass, including the redirect and rebinding cases
- [ ] `NFR-P-04` met on the `medium` preset
- [ ] Every error code has a contributor-readable message and a UI mapping

---

## 14. Task breakdown

| Task | Description | Est (d) | Deps | Phase |
| --- | --- | --- | --- | --- |
| T-M07-01 | **SPIKE: PDF parser evaluation + licence decision** | 3.0 | — | 2 |
| T-M07-02 | Parser registry, `ParsedDocument`, `Block` model | 1.0 | | 2 |
| T-M07-03 | PDF parser per spike outcome | 3.0 | T-M07-01 | 2 |
| T-M07-04 | DOCX / PPTX / XLSX / CSV parsers | 2.0 | T-M07-02 | 2 |
| T-M07-05 | HTML / Markdown / TXT / JSON parsers | 1.5 | T-M07-02 | 2 |
| T-M07-06 | Language detection + tokenizer selection | 1.0 | | 2 |
| T-M07-07 | Chunkers: fixed, recursive, markdown | 2.0 | T-M07-02 | 2 |
| T-M07-08 | **`parent_child` chunker** | 1.5 | T-M07-07 | 2 |
| T-M07-09 | Chunk rules: tables, headers, min-size merge, CJK | 1.5 | T-M07-07 | 2 |
| T-M07-10 | Stage handlers: parse, chunk, index + resumability | 2.5 | T-M06 | 2 |
| T-M07-11 | Incremental reprocessing by content hash | 1.5 | T-M07-10 | 2 |
| T-M07-12 | Index writer → M05, with verification | 1.0 | T-M05 | 2 |
| T-M07-13 | Error taxonomy + contributor-facing messages | 1.0 | T-M07-10 | 2 |
| T-M07-14 | Reference corpus + snapshot tests | 2.0 | T-M07-03..05 | 2 |
| T-M07-15 | OCR: detection, RapidOCR, page caching | 2.0 | T-M07-03 | 3 |
| T-M07-16 | Crawler: frontier, scope, robots, sitemap | 3.0 | T-M00-07 | 3 |
| T-M07-17 | Playwright rendering + main-content extraction | 2.0 | T-M07-16 | 3 |
| T-M07-18 | Incremental recrawl + removal detection + report | 2.0 | T-M07-16 | 3 |
| T-M07-19 | Crawl scheduling + politeness | 1.0 | T-M07-16 | 3 |
| T-M07-20 | Archive parser + safety controls | 1.5 | T-M07-02 | 3 |
| T-M07-21 | Semantic chunker | 1.5 | T-M08 | 3 |
| T-M07-22 | Enrichers (summary, keywords, questions) | 2.0 | T-M10 | 4 |
| T-M07-23 | Tests TC-M07-01..27 | 3.5 | all | 2–3 |
| | **Total** | **42.5** (Phase 2: 24, Phase 3: 14.5, Phase 4: 4) | | |
