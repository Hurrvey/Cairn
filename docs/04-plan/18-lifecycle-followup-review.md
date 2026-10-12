# Lifecycle follow-up review

Date: 2026-09-09. Status: HISTORICAL INVESTIGATION NOTES.
September10 update: the bounded fan-out/enrollment/barrier work investigated here is implemented
and independently accepted in document19/ledger L72. Manual reembed, general stage recovery,
purge/retirement and broader cache ordering still remain open. The source observations below describe
checkpoint e9033ba, not the final September10 implementation.
Parent review performed while the single slice-1 implementer works on version snapshots.
This record refines package 16; it does not expand that worker's write scope.

## Source evidence at checkpoint e9033ba

- `apps/worker/main.py::build_worker` installs only platform maintenance on `maintain` and
  document stage handlers on parse/chunk/embed/index. Neither `kb.reindex_fanout` nor
  `chunk.reembed` has a production consumer. A catalog-enqueued task is not a working feature.
- `CatalogIngestionFacade._commit_indexed` can activate a building version when current enrolled
  pending ledgers reach zero. It has no enrollment-complete barrier. Batching fan-out without
  changing this branch could activate after the first batch and strand the remaining documents.
- `register_upload` and `start_revision` choose building before active. During rebuild, a new
  upload therefore belongs to the building index, not automatically to both namespaces. Define
  and test visibility explicitly rather than claiming dual-write behavior.
- `ParseManifest` includes document, source hash, revision AND index version. A validated prior
  parse can save parser work, but its old envelope cannot be reused as the new run's artifact
  pointer: `_verify_manifest_identity` correctly rejects an old index version. Republish an
  immutable new envelope under the new attempt key only after validating original provenance.
- `_commit_indexed` retires a prior version but does not enqueue the delayed drop task used by
  `CatalogService.activate_index_version`. Both switch paths must share retirement semantics.
- `fail_index_version` clears the building pointer without checking it still matches its input
  version. A delayed failure from v2 must not clear a newer v3 build or mark active v1 failed.
- `edit_chunk` task payload currently omits content hash; its dedupe key contains hash but that is
  not an execution fence. Include expected content hash/revision and verify under lock before
  embedding and before external mutation. Parent chunks must remain non-vector.
- `retry_document` includes revision/version identity but increments revision and always parses
  again. Terminal `record_failure` overwrites ledger state with `failed`; durable failure stage
  and retained validated artifacts must drive stage-level resume instead of relying on memory.
- Runtime cache writes are outside the catalog read transaction. Frozen version metadata fixes
  mixed model/namespace DTOs, not publication ordering: an older publisher can still finish after
  a later switch/deletion. Address generation ordering and durable retry in the publication slice.

## Next implementation contract after slice 1 acceptance

1. Add a version-owned enrollment state/cursor and bounded fan-out operation in catalog. Keep task
   claims fenced; return DTOs through the facade rather than importing catalog ORM into ingestion.
2. Allocate each live document revision/version ledger idempotently with its first durable task.
   Commit cursor progress and successor scheduling atomically. Notify only after transaction commit.
3. Gate activation on enrollment completion AND complete expected current document coverage, not
   just the existing ledger count. Explicitly define empty-KB and terminal document failure results.
4. Exercise at least two batches with a crash/replay at the cursor boundary; first-batch completion
   must leave the old active index untouched. Add source replacement, deletion and upload at the
   boundary, including updates behind a cursor. Do not assume UUID order is a creation-time fence.
5. Validate production factory registration and real queue execution. Keep parser-call accounting
   to prove artifact reuse; use actual PostgreSQL/Redis/pgvector and report fake vs real models.

These are planned checks, not passing tests. Lock order, cursor representation, reconciliation
strategy and empty/failure semantics must be settled and recorded before the next implementation.
