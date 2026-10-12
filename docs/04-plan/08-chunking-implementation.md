# M07 chunking increment

**Date:** 2026-09-07 · **Status:** implemented and tested; worker wiring remains open.

## Scope and implementation sequence

Continue T-M07-07, T-M07-08 and T-M07-09 against the existing M07 §5 design.
The parser DTOs and M08 tokenizer contract are available in the working tree;
neither a PDF engine nor worker orchestration is required for this increment.

1. Reuse `ChunkConfig`, the catalog's `ChunkSpec` and the model `Tokenizer`.
   Bind document identity and index version in `DocumentChunker` construction.
2. Implement fixed, recursive, Markdown and parent-child strategies, preserving
   protected tables, heading context and exact source block ranges.
3. Exercise TC-M07-06..12 with offline real tiktoken/Hugging Face tokenizers;
   regress token-boundary correctness and excessive repeated tokenization.
4. Round-trip parser/chunker output and replay through the real PostgreSQL
   catalog. Run backend tests/coverage and static boundary checks.
5. Record results and remaining gates without claiming worker end-to-end completion.

## Contract and rules

- `DocumentChunker(document_id=..., tokenizer=..., index_version=1).chunk(doc, cfg)`
  returns catalog write specifications. No database, network or model-loading
  operations occur during chunking; the tokenizer must be explicitly prepared.
- All strategies keep heading changes and protected tables as hard boundaries;
  repeated headings still delimit distinct sections. Fixed uses token windows;
  recursive, Markdown and parent-child prefer configured separators, then CJK
  and Latin sentence punctuation, then Unicode-safe token boundaries.
- The heading prefix is `Title > Subtitle\n\n`. Token counts include this prefix
  and tokenizer special tokens. Disabling the prefix retains heading metadata.
  Parent windows use `parent_tokens`; all searchable windows use `child_tokens`.
- Small leading fragments pack forward within their section when budget allows.
  Terminal fragments and isolated short sections may remain below the minimum:
  the minimum never overrides token budgets, heading boundaries or table isolation.
- Overlap is a forward-tokenized suffix with an upper bound of `child_overlap`.
  It may shrink at Unicode/token boundaries and is dropped when it would prevent
  progress. Parent windows do not overlap, and children stay inside their parent.
- `metadata.role` distinguishes `parent`, `child` and `standalone`;
  `metadata.embed` is false only for parents. A future embedding handler must
  honor this marker rather than embedding every row with `parent_id IS NULL`.
- An oversized protected table stays intact and carries `oversized=true` and
  `oversized_reason=table`. This is not permission for a future embedding stage
  to silently truncate it: that stage still needs an explicit oversize policy.
- Citations retain each contributing block's ordinal, kind, heading, page, bbox,
  source URL, and half-open Python character offsets into the original block text.
  The top-level page/URL select the first contributing block; full citations retain
  all contributing pages and URLs. Unknown fields stay null, not fabricated.
- UUIDv5 IDs are deterministic for document/index/config/tokenizer identity,
  ordinal, content and citations. Content hashes cover exactly the emitted UTF-8
  content. Identical text at different positions has distinct row IDs but can
  share a content hash. This is not the incremental re-embedding implementation.
- `semantic` and `custom` fail with `CHUNK_UNSUPPORTED_STRATEGY`, never a silent
  fallback. An impossible token budget fails with `CHUNK_BUDGET_EXCEEDED`.
- CPU work runs off the event loop. Metrics use bounded strategy/outcome labels;
  logs include counts, not source content. Process time/memory isolation remains
  a requirement for future worker composition.

## Remaining gates

PDF's 30-document annotated corpus and quality measurements, Office/OCR parsing,
automatic language/tokenizer selection, semantic/custom strategies, worker
parse/chunk/embed/index handlers, resumability, incremental reprocessing and index
verification remain open. The catalog round-trip test is not a worker E2E test.
FR-F-05 as a whole and the Phase 2 knowledge-base end-to-end gate remain open.

## Integration finding

The PostgreSQL round-trip initially returned empty metadata for every chunk.
`CatalogService.replace_chunks` supplied the database column name `metadata`
to SQLAlchemy ORM bulk insert, which expects the mapped attribute name
`chunk_metadata`. Correcting that key preserves citations and parent/embed roles.
The integration test verifies both the initial write and idempotent replay.

The BPE regression tests also reproduced separator cuts that increased token
counts, a valid header/body merge rejected by prefix-only counting, and excessive
whole-document tokenization. All were corrected before final verification.
Incremental probing now bounds the text passed to the tokenizer per window;
source-range lookup uses ordered block intervals. This is an algorithmic
regression check, not a measured production throughput/RSS acceptance result.

Existing M08 files, their tests and related core files were formatted and imports
sorted to restore the repository's static gates; their runtime behavior was not
changed by this formatting pass. Existing user changes remain in the working tree.

## Verification results

Python 3.13.5, dedicated PostgreSQL 16/pgvector and Redis 7.4 Docker services.
Both services bind to random loopback ports; PostgreSQL data lives on tmpfs.
This verifies real service behavior, not disk/crash durability. The two containers
created for this increment are `cairn-chunk-verify-pg` and `cairn-chunk-verify-redis`.
Both were removed after verification. Other pre-existing containers were untouched.

| Check | Result |
| --- | --- |
| Full backend suite | **510 passed**, no skips, 79.05 seconds |
| Breakdown | 252 unit + 137 contract + 121 integration |
| New chunking tests | 45 unit + 1 PostgreSQL catalog round-trip/replay |
| Combined statement/branch coverage | **82.87%**, existing 80% gate passes |
| Ruff lint/format | Pass, 137 Python files formatted |
| Strict mypy | Pass, 99 source files |
| Import boundaries | 5 contracts kept, 0 broken |
| Route authorization audit | 50 routes, 8 intentionally public |

A final read-only review found no substantive correctness issues in this increment.
Its three test-strengthening suggestions were applied: configuration/tokenizer
fingerprint identity changes, cross-page parent/child citation containment, and
independent checks immediately after the initial database write and after replay.
The final focused rerun passed **119 tests** (all ingestion unit tests and catalog
integration tests) in 17.94 seconds. These changes added assertions to existing
tests; the full-suite count remains 510. The full-suite/coverage figures above
precede this assertion-only strengthening; runtime behavior did not change.

The final backend command was:

```powershell
uv run python -m pytest tests --cov --cov-report=term --cov-fail-under=80
```

Service URLs were explicitly set with `CAIRN_TEST_USE_EXTERNAL_SERVICES=1` for
these disposable containers. Do not point the truncating fixtures at application
data. One existing Alembic `path_separator` deprecation warning remains.
No frontend, browser E2E, PDF quality corpus, model parity/GPU throughput,
production worker performance or application image build is claimed by this run.
