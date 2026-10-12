# Query-time observational evaluation

Status: direction approved October8 by the persistent `/root/design_advisor`; queued after
SDK/quota/source-lifecycle slices. Coordinator designs, fixed developer implements, same
reviewer accepts. This increment does not close all M14 requirements.

## Deliverable and evidence model

Deliver reviewed golden-set management/import, query-time runs, retrieval metrics, per-query
evidence, exploratory comparison/export and UI. Reject chunking/embedding reindex experiments
until the temporary-index lifecycle is separately implemented. Do not alter production ACTIVE
to run an experiment.

A run stores an immutable snapshot of approved golden items, queries, relevance judgments,
metric rules and k values. Golden-set edits/deletion never mutate historical run evidence.
Record full effective retrieval configuration, model/reranker identities and evaluator/software
version, including resolved KB defaults rather than only request overrides.

Each query records timing, status, actual index identities, ordered hit IDs and native scores,
available revisions/content hashes, degradation, partial failures, latency and safe error codes.
Evidence must suffice to recompute metrics without reading current chunk content. Do not retain
result bodies by default when IDs/judgments/hashes suffice. Freeze the successful result on retry;
task identity and query identity make completion idempotent. Cancellation preserves completed
items and marks the run incomplete, without silently replacing results from an earlier attempt.

## Consistency and comparison

First-slice runs are explicitly `observational`, with consistency `unverified` or
`change_detected`. Equal index versions are not proof of frozen content: manual chunk edits,
reembedding, deletions and runtime-default changes can alter retrieval within one index version.
Failure to detect change remains unverified, never stable.

Compare by stable golden-item identity and show exploratory deltas plus coverage. Distinct golden
snapshots, missing queries or different corpus identities must be visible. Do not show statistical
significance or claim a controlled paired experiment in this slice. Successful empty results,
failed executions, degraded responses and partial failures remain distinct; report denominators
and exclusions explicitly, rather than turning errors into zero relevance scores.

Chunk-ID judgments are bound to a known index. Invalid/stale references are reported separately
from retrieval misses. Rechunking experiments require source-span/document identities or explicit
lineage before their metrics can be meaningful.

## Stronger consistency is a separate change

A later corpus-generation protocol must fence external writes, not merely increment a SQL
counter after success. Commit a durable pending mutation before a retrievable write; keep it
pending/uncertain until external state and publication are verified, then settle and advance the
generation. Cover indexing, edits, replacements, deletion, index switches, compensation and late
worker recovery. Run boundaries require no pending/uncertain mutations and unchanged generation,
ACTIVE identity and effective configuration. Both compared runs require the same corpus and
golden snapshots. No long SQL lock or global ban on online edits is required; concurrent changes
make a run unverifiable. Immutable temporary snapshots remain a separate stronger option.

## Minimum acceptance before implementation completion

- Golden snapshot and completed metrics remain unchanged after later golden-item edits/deletion;
  unapproved generated items are excluded.
- Hand-computed fixtures specify graded nDCG, MRR, recall, precision and hit rate, including zero
  relevant judgments, duplicate hits and fewer than k results; reference-library verification is
  an additional M14 gate, not inferred from internal tests.
- Query failures/partial/degraded/empty success are distinguished, with coverage and exclusions.
- ACTIVE/config changes and available same-version revision/hash changes preserve evidence;
  undetectable changes never upgrade observational/unverified to stable.
- Comparisons pair stable item IDs, surface incompatible snapshots and omit unsupported
  significance claims.
- Retries/cancellation/resume retain completed evidence without double accumulation.
- Golden sets, runs and KB references enforce tenant/permission boundaries. Recheck current
  authorization when executing background work and exporting; revocation/KB deletion has an
  explicit stop/error outcome and never bypasses reading permissions through retained results.

Before coding, settle exact schema/import limits, metric conventions, permission inheritance,
worker recovery and browser acceptance in an implementation plan reviewed by the same advisor.
