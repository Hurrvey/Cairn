# ADR-0003 — PostgreSQL-backed durable task queue, not a message broker

**Status:** Accepted · **Date:** 2026-08-28 · **Deciders:** Architecture
**Supersedes:** an earlier Celery + Redis recommendation

## Context

Ingestion is a multi-stage background pipeline: fetch → parse → chunk → embed → index. It must
be resumable (`NFR-R-05`), idempotent (`NFR-R-06`), fair across workspaces, observable per
document (`NFR-O-05`), and lossless under worker crashes (`NFR-R-04`).

The obvious choice is Celery + Redis. It has a specific, serious flaw for this workload.

### The dual-write problem

With a broker, creating a document and enqueuing its work are **two writes to two systems**:

```python
document = await repo.create(...)      # committed to Postgres
await session.commit()
parse_document.delay(document.id)      # separate system — may never happen
```

A crash, a network partition, or a Redis failure between those lines leaves a document in
`registered` forever, with no task, no error, and **nothing that can detect it**. The reverse
ordering produces tasks referencing documents that do not exist. Outbox patterns fix it — by
introducing exactly the durable database table this ADR proposes, plus a relay process.

Empirically this failure mode surfaces months after launch as "some documents just never
finish," is unreproducible, and is the single most common defect class in broker-based
ingestion pipelines.

## Decision

Use a **`task` table in PostgreSQL** with `SELECT … FOR UPDATE SKIP LOCKED` claiming, leases
with heartbeats, and `LISTEN/NOTIFY` wakeups.

```sql
WITH picked AS (
  SELECT t.id FROM task t
  JOIN workspace_runtime wr ON wr.workspace_id = t.workspace_id
  WHERE t.queue = $1 AND t.state = 'ready' AND t.run_after <= now()
    AND wr.running < wr.concurrency_limit          -- fair scheduling
  ORDER BY t.priority DESC, t.id
  LIMIT $2
  FOR UPDATE OF t SKIP LOCKED
)
UPDATE task t SET state='running', attempt=attempt+1,
                  worker_id=$3, lease_until=now()+$4::interval
  FROM picked p WHERE t.id = p.id
RETURNING t.*;
```

Enqueue happens **in the same transaction** as the state change that requires it (`NFR-R-03`).

Implementation may use `procrastinate` (Postgres-native, asyncio, LISTEN/NOTIFY) or ~300 lines
hand-rolled. Redis remains, for caching and rate limiting only — never as a source of truth.

## Consequences

**Positive**
- **Transactional enqueue.** The dual-write bug class is structurally impossible.
- Job state is SQL-queryable — the per-document progress UI (`FR-P-05`) is a `SELECT`, not a
  Flower scrape.
- Fair scheduling is an `ORDER BY` plus a join, instead of per-tenant queue proliferation.
- Retries, backoff, dead-lettering, and cancellation are `UPDATE`s.
- One fewer system to deploy, monitor, secure, and back up.
- Exactly-once claiming from `SKIP LOCKED` with no coordination.
- KEDA autoscales on `SELECT count(*) … WHERE state='ready'` — the correct signal.

**Negative**
- Adds write load to Postgres. Quantified: at 1000 tasks/min, ~50 writes/s — negligible.
- The `task` table needs vacuum attention. Mitigated: partial indexes keep the hot set small,
  and `worker-maintain` prunes completed rows after 7 days.
- Throughput ceiling ~5–10k claims/s. Document ingestion peaks around 100/s. **~50× headroom.**
- No Flower UI. Mitigated: we build a better one from SQL, which we needed anyway.

## Implementation requirements

1. Partial index `ix_task_claim ON task(queue, priority DESC, id) WHERE state='ready'` — keeps
   the index small regardless of table size.
2. Leases with heartbeat; a reaper resets `state='ready'` where `lease_until < now()`.
3. `workspace_runtime.running` maintained in the claim and finish transactions — a correlated
   subquery counting running tasks per workspace is too slow at scale.
4. `dedupe_key` with a partial unique index prevents duplicate in-flight work.
5. Batch claiming (`LIMIT 64`) for the embed queue so GPU batches fill.

## Alternatives rejected

| Alternative | Why not |
| --- | --- |
| Celery + Redis/RabbitMQ | Dual-write bug class; opaque state; painful fair scheduling |
| Celery + transactional outbox | The outbox *is* this table, plus a relay process to operate |
| Temporal | Excellent durable execution, but a major operational commitment for a pipeline that is 6 linear stages |
| Redis Streams | Better than pub/sub, still a separate system with the dual-write problem |
| SQS/Cloud queues | Couples self-hosted deployments to a cloud provider |
