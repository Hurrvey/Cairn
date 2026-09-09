# Ingestion completion coordination

**Date:** 2026-09-08 · **Status:** implementation and independent acceptance in progress.

Current restart point: [execution ledger](15-execution-ledger.md). It records the interrupted repair
package, independently verified Office results, failed pipeline acceptance and remaining work.

The user requested implementation of the remaining ingestion scope with
`gpt-5.6-sol`, reasoning effort `high`, under a coordinating reviewer. Existing
working-tree changes are preserved; no commits, branches or application-data
operations are part of this continuation.

**Scheduling update:** the user subsequently required one running subagent at a
time. All workers except the pipeline worker were stopped immediately. Their
partial working-tree files remain unverified. Subsequent work resumes serially,
with coordinator acceptance before switching to the next worker; each worker
continues to use `gpt-5.6-sol` with `high` reasoning effort.

## Work packages

| Owner | Write scope | Planned delivery |
| --- | --- | --- |
| Pipeline worker | New ingestion pipeline/runtime/artifacts, catalog facade and migration, worker entrypoint, dedicated tests | Parse/chunk/embed/index, transactional chaining, revision/index fencing, recovery and incremental processing |
| Office/language worker | Office and language adapters and dedicated tests | DOCX/PPTX/XLSX/CSV, bounded package inspection, automatic language and explicit model-tokenizer selection |
| Advanced-chunking worker | Chunker dispatch, semantic/custom modules and tests | Actual semantic boundaries and validated, version-pinned custom execution |
| PDF/OCR worker | PDF/OCR modules, corpus acquisition/evaluation scripts and dedicated tests | Engine-backed parsing/OCR, genuine 30-document quality evidence or a precise remaining gate |
| Task-fencing worker | Task repository/worker/service and dedicated tests | Stale attempts cannot heartbeat, finish, reschedule or commit another worker's lease |
| Coordinator | Dependency declarations, registry composition, boundary contracts, entrypoint acceptance and shared docs | Interface arbitration, independent integration tests, static checks, service-backed acceptance and cleanup |

## Acceptance rules

1. Existing module contracts and source/citation fidelity are mandatory. No direct
   cross-module ORM access and no untrusted Python execution in the application.
2. A stage transition and its successor task commit together. A stale revision,
   expired lease, deleted document or obsolete index cannot publish results.
3. Parents are not embedded. Token limits include headings and special tokens;
   oversized tables require an explicit non-truncating policy.
4. Incremental tests compare provider calls and stored vectors, not just task
   states. Partial vector writes cannot be reported as a successful index.
5. Office/PDF/OCR tests use real engine execution where available, including
   hostile/corrupt input. Automatic detection never replaces a configured
   embedding model's tokenizer with a guessed tokenizer.
6. PDF evaluation uses 30 redistributable real documents with pinned source
   identities and independent annotations. Synthetic fixtures and matching an
   engine's output against itself cannot satisfy the quality gate.
7. Unit tests, real PostgreSQL/pgvector + Redis integration, strict mypy, Ruff,
   import boundaries and authorization audits must pass without lowered gates.
8. Report executable integration separately from production engine/model,
   sandbox, corpus-quality and deployment acceptance. No unsupported completion
   claims, including for the broader project's later retrieval/UI phases.

## Service isolation

This continuation creates only `cairn-completion-pg` and
`cairn-completion-redis`, labelled `cairn.task=completion-20260908`, bound to
loopback random ports. PostgreSQL data uses tmpfs, not an application volume.
Separate `cairn_pipeline`, `cairn_fencing` and `cairn_acceptance` databases and
Redis databases isolate independently run, truncating test suites. Only the
coordinator removes these containers after tests; pre-existing containers remain
untouched. This is genuine database execution, not disk/crash-durability proof.
