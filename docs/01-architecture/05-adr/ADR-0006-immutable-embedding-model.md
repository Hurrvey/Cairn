# ADR-0006 — Embedding model is immutable per knowledge base

**Status:** Accepted · **Date:** 2026-08-28 · **Deciders:** Architecture

## Context

A knowledge base is configured with an embedding model (`FR-C-04`). What happens if a user
changes it after documents are indexed?

If existing vectors are kept and new documents are embedded with the new model, the index
contains vectors from **two different embedding spaces**. Cosine similarity between them is
meaningless — not an error, just noise. The failure is silent and severe:

- No exception. No warning. Scores stay in a plausible range.
- Retrieval quality degrades for a subset of content, unpredictably.
- The symptom ("sometimes it doesn't find the obvious document") is nearly impossible to trace
  back to the cause weeks later.
- Every downstream metric — rerank scores, thresholds, evaluation results — becomes unreliable.

This is one of the most damaging bugs a RAG system can have, precisely because it never
announces itself.

## Decision

`knowledge_base.embedding_model_id`, `embedding_dim`, and `metric` are **immutable after the
first successful index build**. Enforced at three layers:

1. **API** — `PATCH /v1/knowledge-bases/{id}` rejects these fields once
   `active_index_version IS NOT NULL`, with `EMBEDDING_MODEL_IMMUTABLE`.
2. **Service** — `CatalogService.update()` raises before persisting.
3. **Database** — a trigger blocks the update, so no code path can bypass it.

```sql
CREATE OR REPLACE FUNCTION guard_kb_embedding_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  -- Nothing embedded yet, so there is no space to be inconsistent with.
  IF OLD.active_index_version IS NULL THEN
    RETURN NEW;
  END IF;

  IF NEW.embedding_model_id IS NOT DISTINCT FROM OLD.embedding_model_id
     AND NEW.embedding_dim IS NOT DISTINCT FROM OLD.embedding_dim
     AND NEW.metric IS NOT DISTINCT FROM OLD.metric THEN
    RETURN NEW;
  END IF;

  -- THE CARVE-OUT. A change is permitted only when the same statement opens a
  -- new index build, which is the reindex path below. Without this clause the
  -- trigger would block the one sanctioned way to change the model, and the
  -- backstop would be preventing the very operation it exists to protect.
  IF NEW.building_index_version IS NOT NULL
     AND NEW.building_index_version IS DISTINCT FROM OLD.building_index_version THEN
    RETURN NEW;
  END IF;

  RAISE EXCEPTION
    USING ERRCODE = '23514',   -- check_violation
          MESSAGE = 'embedding configuration is immutable for an indexed knowledge base',
          HINT    = 'Start a rebuild instead, which opens a new index version.';
END;
$$;

CREATE TRIGGER trg_kb_embedding_immutable
  BEFORE UPDATE OF embedding_model_id, embedding_dim, metric ON knowledge_base
  FOR EACH ROW EXECUTE FUNCTION guard_kb_embedding_immutable();
```

The carve-out is narrow enough to keep the invariant intact: opening a build sets
`building_index_version` to a *new* value, and the rebuild writes into a fresh namespace
(`(kb_id, building_index_version)`). The active namespace is never touched, so the two
embedding spaces never coexist inside one index. `BEFORE UPDATE OF …` also keeps the
trigger off every unrelated `knowledge_base` write — counter bumps and status changes are
frequent, and paying a plpgsql call for each of them would be pure overhead.

**Changing the model is possible** — as an explicit, different operation:
`POST /v1/knowledge-bases/{id}/reindex` with a new `embedding_model_id`, which triggers the
blue/green rebuild of ADR-0007. The user is told it is a full rebuild, shown an estimated
duration and token cost, and must confirm.

Additionally, every write validates vector dimension against the KB configuration (`FR-G-04`).
A mismatch raises `EMBEDDING_DIMENSION_MISMATCH`. **Never truncate, never pad** — silent
dimension coercion is the same class of bug.

## Consequences

**Positive**
- The single most damaging silent-corruption mode is structurally impossible.
- Every vector in a namespace is guaranteed comparable.
- Evaluation results are trustworthy because the index is homogeneous.
- The user gets an honest, informed choice instead of an invisible consequence.

**Negative**
- Changing models is a heavyweight operation (full re-embed: time and provider cost).
  **This reflects reality** — the operation genuinely is expensive; hiding that would be worse.
- Three enforcement layers is redundancy. Deliberate: the database trigger is the backstop
  against a future code path nobody reviewed.

## Implementation requirements

1. Reindex-with-new-model must show, before confirmation: chunk count, estimated tokens,
   estimated cost (from `model.cost_per_1k_input`), and estimated wall-clock duration.
2. Blue/green means the old index serves throughout (ADR-0007) — this is what makes the
   operation acceptable in production.
3. `EMBEDDING_MODEL_IMMUTABLE` error detail must explain the reindex path, not just refuse.
4. The evaluation module (`M14`) uses temporary index versions for model comparison, so
   experimentation does not require mutating the KB (`FR-N-06`).

## Alternatives rejected

| Alternative | Why not |
| --- | --- |
| Allow the change, warn the user | Warnings are dismissed; the corruption is permanent and silent |
| Auto-reindex on change | A UI click silently triggering hours of billable compute is a footgun |
| Store `model_id` per chunk, filter at query time | Splits the index; you can only ever search one subset; complexity with no upside |
| Multi-space search then merge | Scores from different spaces are not comparable; merging is meaningless |
