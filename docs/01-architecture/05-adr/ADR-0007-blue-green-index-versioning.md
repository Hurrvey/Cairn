# ADR-0007 — Blue/green index versioning

**Status:** Accepted · **Date:** 2026-08-28 · **Deciders:** Architecture

## Context

Several routine operations require rebuilding a knowledge base's entire vector index:

- Changing chunking strategy or size (`FR-C-05`)
- Changing the embedding model (ADR-0006)
- Migrating to a different vector backend (`FR-C-03`)
- Recovering from read-model drift (ADR-0005)
- Evaluating a candidate configuration (`FR-N-06`)

Rebuilding in place means either taking retrieval down for the duration — hours for a large KB —
or serving a partially-rebuilt index, which returns wrong results without saying so.

The practical consequence of getting this wrong is that **users stop tuning**. If changing chunk
size means an outage, nobody changes chunk size, and retrieval quality stays wherever it landed
on day one. This decision is as much about product behaviour as about availability.

## Decision

Every KB carries a monotonically increasing `index_version`. Vectors are addressed by
`Namespace = (kb_id, index_version)`. A rebuild writes a **new version** while the **active
version keeps serving**, then switches atomically.

```
knowledge_base.active_index_version    -- what retrieval reads
knowledge_base.building_index_version  -- what the rebuild writes
```

### Rebuild sequence

```
1. Allocate:   building_index_version = COALESCE(active, 0) + 1
               INSERT kb_index_version (state='building')
2. Prepare:    VectorStore.ensure_namespace((kb_id, v_new), spec)
3. Fan out:    one `embed` + `index` task batch per document → namespace v_new
               retrieval continues serving from v_active throughout
4. Verify:     count(v_new) == expected chunk count; sample-search sanity check
5. Switch:     UPDATE knowledge_base
                  SET active_index_version = building_index_version,
                      building_index_version = NULL,
                      config_version = config_version + 1     -- invalidates the cache
               UPDATE kb_index_version SET state='active'  WHERE version = v_new
               UPDATE kb_index_version SET state='retired', retire_after = now() + '24h'
                                        WHERE version = v_old
6. Reclaim:    after retire_after, worker-maintain drops namespace (kb_id, v_old)
```

Step 5 is a single transaction. The switch is atomic from the caller's perspective: a request
either reads the old index or the new one, never a mixture.

## Consequences

**Positive**
- **Zero-downtime reindex.** Reconfiguring a 500k-document KB is a background operation.
- **Safe rollback.** Until `retire_after` elapses, reverting is one UPDATE.
- Evaluation can build a temporary version, measure, and discard (`FR-N-06`).
- Backend migration is the same mechanism with a different storage binding.
- Read-model drift repair is just a rebuild.

**Negative**
- **Peak storage is roughly 2×** during a rebuild. Documented in the sizing guide; the UI shows
  projected peak usage and refuses to start if the quota would be exceeded.
- Rebuild costs a full re-embed. Mitigated: the embedding cache (`FR-G-02`) makes an
  unchanged-content rebuild nearly free of provider calls — only chunking changes force new
  embeddings.
- Two extra columns of state to reason about. Mitigated: `Namespace` hides it from every caller
  except `M03` and `M05`.

## Implementation requirements

1. **Retrieval always reads `active_index_version`.** Never "latest". A query during a rebuild
   must be unable to see partial data. This is enforced by making `Namespace` construction go
   through one function that reads only the active version.
2. `config_version` bumps on switch so the data-plane cache picks up the new namespace within
   its TTL. For deletion and switch, also publish an explicit Redis invalidation message for
   immediate effect.
3. Progress is reported per version in `kb_index_version.chunk_done / chunk_total` (`FR-G-09`).
4. A failed rebuild sets `state='failed'`, leaves `active_index_version` untouched, and drops
   the partial namespace. **The active index is never at risk.**
5. Concurrent rebuilds of the same KB are rejected with `KB_INDEX_IN_PROGRESS` — enforced by a
   partial unique index on `kb_index_version(kb_id) WHERE state='building'`.
6. Quota check before allocation: refuse if `2 × current_vector_bytes` exceeds the workspace
   quota.

## Alternatives rejected

| Alternative | Why not |
| --- | --- |
| In-place rebuild | Downtime or silently partial results |
| Delete-then-rebuild | Guaranteed outage; unrecoverable on failure |
| Dual-write during migration | Complex, and does not solve chunking changes where chunk identity itself changes |
| Version at the chunk level, filter at query time | Every query pays a filter cost forever; and the old vectors are never reclaimed |
