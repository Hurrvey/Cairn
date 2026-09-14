# Pipeline lifecycle acceptance package

Updated: 2026-09-14. Status: IN PROGRESS. Bounded slices1-3 independently ACCEPTED;
slices4-5 and full E2E remain OPEN. Prerequisite repair was accepted September8.

September14: manual chunk reembedding passed101 independent focused tests including actual TEI
manual-edit retrieval. Final backend761 passed,2 known PDF/OCR collection errors,no assertion
failures/skips,coverage85.18%; document20/ledger L74-L87. Next is durable failed-stage recovery.
Parent edits are context-only; edits during an existing rebuild/committed ordinary manifests are
rejected rather than allowing conflicting writers. Legacy incomplete edit tasks need re-editing.

September10: fan-out/enrollment barriers, current-document coverage, build-scoped deletion cleanup,
replay/publication retry and valid/fallback parse reuse passed 40 independent focused tests. Full
backend: 734 passed, 2 known PDF/OCR collection errors, no assertion failures/skips, coverage85.05%.
See document19 and ledger L54-L72. Next implement manual chunk reembedding (slice3); do not mistake
build-only deletion cleanup for general purge or delayed index retirement.

September9 evidence: coordinator independently passed 86 focused tests including the original two
regressions, four additional isolation/history cases, snapshot/migration tests and real TEI
markdown/semantic pipeline. See ledger L42-L50 and `17-version-snapshot-implementation.md`.
Final full backend: 703 passed, 2 known PDF/OCR collection errors, no assertion failures/skips,
84.83% coverage; see ledger L52. The complete backend command remains nonzero.
The new snapshot is credential-free registered metadata, not cryptographic evidence of provider
weights or tokenizer bytes. Legacy versions intentionally have unavailable provenance and require
an explicit drained upgrade/rebuild procedure. Full lifecycle and knowledge-base E2E remain OPEN.

Historical pause checkpoint: prerequisite bounded repair was accepted September 8. On September 9 the
coordinator independently reproduced both lifecycle failures on fresh PostgreSQL/pgvector and Redis:
**2 failed in 17.70s**; report `data/acceptance-20260909/lifecycle-red.xml`. No implementation agent
was dispatched before the user requested a pause. Both September 9 disposable containers were
ownership-verified and removed. Resume with vertical slice 1 below; see ledger L39-L41 for exact
state, commands and ownership constraints. No knowledge-base E2E acceptance is implied.

## Existing evidence and gaps

Historical pre-slice evidence: catalog enqueued `kb.reindex_fanout` and `chunk.reembed` before
production worker consumers existed. Both are now implemented and bounded-accepted in documents19/20;
a successful initial upload alone was not the acceptance criterion. See M03 section4.3,
M07 T-M07-10/11/12 and ADR-0007 for remaining full-lifecycle requirements.

Inspection also found that `start_reindex` changes the KB model immediately, while
`publish_runtime` combines the KB model with the old active namespace. A rebuild must not cause
queries against old vectors to use a different model/dimension. Coordinator subsequently added
`tests/integration/test_pipeline_lifecycle_acceptance.py`: **2 failed** on real PostgreSQL/pgvector
and Redis (2026-09-08). The model/namespace mismatch is reproduced, as is reuse of a failed version
number causing a primary-key conflict. See execution ledger L29. Both are now repaired in slice 1
and passed the coordinator's focused rerun; this does not establish fan-out or rebuild completion.

## Required vertical slices

1. **Version-owned configuration.** Freeze model identity/dimension, metric, tokenizer and chunk
   settings for each building version. Active runtime stays bound to its active version. Allocate
   versions monotonically even after failed or retired builds. Migrate existing versions safely.
2. **Resumable fan-out.** A maintain handler creates revision/version-scoped ledgers and successor
   tasks in bounded batches. Persist cursor/completion state. No premature activation while a
   later batch is undispatched; retry cannot duplicate work. Reuse valid parse artifacts instead
   of reparsing unnecessarily. New uploads, replacements and deletions during rebuild must be
   assigned a coherent target and cannot strand activation.
3. **Manual chunk re-embedding.** Consume only the edited chunk's version/hash. Validate exact
   model token budget, preserve user text/citations, reject stale attempts/edits, and verify the
   vector payload before success. Editing a parent must not accidentally index a parent vector.
4. **Recovery entrypoints.** Contributor retry resumes the durable failed stage; it must not
   enqueue payloads missing revision/index identity or rerun completed work unnecessarily.
5. **Activation and retirement.** Verify expected live documents and point identities before the
   switch. Mark failed builds explicitly without damaging the old active index. Schedule delayed
   old-namespace retirement through the existing maintenance seam, fenced against deleting an
   active or building namespace. Retry cache publication independently after a committed switch.

## Acceptance tests (real PostgreSQL, Redis and pgvector)

| Case | Decisive assertions |
| --- | --- |
| Model A -> B rebuild | Old namespace/model A remains coherent until switch; new points/model B afterward |
| Multi-batch fan-out interruption | Cursor resumes; each live document has one ledger; no early activation |
| Replayed fan-out task | No duplicate stage tasks, points or counters |
| Failed build followed by rebuild | Old active remains queryable; new unique version allocated |
| Empty KB rebuild | Explicit empty completion or documented rejection; never a permanently building KB |
| Changed/replaced source mid-build | No obsolete revision can publish; new revision is not lost |
| Deleted document mid-build | Deleted source cannot publish; expected counts converge |
| Edit one child chunk | Only changed content embedded; vector hash/text equals catalog edit |
| Edit again before old task runs | Old task cannot overwrite the latest edit |
| Parent edit | Parent stays non-vector; documented effect on children |
| Retry at each failed stage | Correct identity payload and resume point; stable non-duplicated side effects |
| Delayed retirement | Old namespace removed only after retention; active/building namespaces untouched |
| Runtime publication failure | Retry republishes committed active version without re-embedding |

## Boundaries and review gates

- Catalog owns persistence and returns DTOs; ingestion does not import another module's ORM.
- Model and storage credentials remain outside task payloads and durable public artifacts.
- No queue kind is accepted as supported merely because the catalog can enqueue it.
- Test the production worker factory, not only a hand-assembled handler map.
- Document remote vector-driver consistency limitations; do not claim transactional remote writes.
- Update package docs with red/green evidence and final command output summaries before handoff.
- Coordinator independently reruns tests and reviews migration/locking/publication before acceptance.
