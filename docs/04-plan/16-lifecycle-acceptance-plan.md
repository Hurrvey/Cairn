# Pipeline lifecycle acceptance package

Updated: 2026-09-09. Status: PAUSED, still PLANNED; not dispatched or implemented.
Prerequisite: accept the bounded repair package tracked in `15-execution-ledger.md`.

Pause checkpoint: prerequisite bounded repair was accepted September 8. On September 9 the
coordinator independently reproduced both lifecycle failures on fresh PostgreSQL/pgvector and Redis:
**2 failed in 17.70s**; report `data/acceptance-20260909/lifecycle-red.xml`. No implementation agent
was dispatched before the user requested a pause. Both September 9 disposable containers were
ownership-verified and removed. Resume with vertical slice 1 below; see ledger L39-L41 for exact
state, commands and ownership constraints. No knowledge-base E2E acceptance is implied.

## Existing evidence and gaps

The catalog currently enqueues `kb.reindex_fanout` and `chunk.reembed`, but the four-stage ingestion
worker registration does not consume those kinds. A successful initial upload does not complete
reindex or manual-edit processing. See M03 section 4.3, M07 T-M07-10/11/12 and ADR-0007.

Inspection also found that `start_reindex` changes the KB model immediately, while
`publish_runtime` combines the KB model with the old active namespace. A rebuild must not cause
queries against old vectors to use a different model/dimension. Coordinator subsequently added
`tests/integration/test_pipeline_lifecycle_acceptance.py`: **2 failed** on real PostgreSQL/pgvector
and Redis (2026-09-08). The model/namespace mismatch is reproduced, as is reuse of a failed version
number causing a primary-key conflict. See execution ledger L29. Neither failure is repaired yet.

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
