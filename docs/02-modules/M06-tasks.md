# M06 — Durable Task Queue

| | |
| --- | --- |
| **Package** | `cairn.tasks` |
| **Layer** | L2 domain service |
| **Phase** | 1 |
| **Owner** | Backend lead |
| **Depends on** | M00 only |
| **Depended on by** | M03, M07, M11, M12, M14, M15, apps/worker |
| **Tables owned** | `task`, `workspace_runtime` |
| **Requirements owned** | FR-G-09, NFR-R-03, NFR-R-04, NFR-R-05, NFR-R-06, NFR-S-02, NFR-S-06 |
| **ADR** | [ADR-0003](../01-architecture/05-adr/ADR-0003-postgres-task-queue.md) |

---

## 1. Purpose and scope

A durable, fair, resumable background-work system built on PostgreSQL.

**In scope:** enqueue (transactional), claim (`SKIP LOCKED`), leases and heartbeats, retries and
backoff, dead-lettering, cancellation, fair scheduling, the worker harness, queue metrics,
reaping and pruning.

**Out of scope:** what the tasks *do* (M07, M11, M14) and cron scheduling of crawls (M07 owns
`crawl_job.schedule_cron`; M06 provides the periodic trigger primitive).

---

## 2. Queues

| Queue | Bound by | Pool | Concurrency | Lease | Notes |
| --- | --- | --- | --- | --- | --- |
| `fetch` | network I/O | asyncio | 100–200 | 5 min | crawl, download |
| `parse` | CPU | process | = cores | 15 min | **the usual bottleneck** |
| `ocr` | CPU/GPU | process | 2–4 | 30 min | isolated so one scanned PDF cannot eat the parse pool |
| `chunk` | CPU (light) | asyncio | 16 | 5 min | |
| `embed` | GPU / provider rate | asyncio + batching | 4–8, batch 64 | 10 min | batch across documents |
| `index` | network I/O | asyncio | 16–32 | 10 min | vector upsert |
| `maintain` | mixed | process | 1–2 | 60 min | recrawl, GC, reindex fan-out, partitions |

Each queue is a **separate Deployment** with its own replica count and autoscaling
(`NFR-S-02`). One undifferentiated pool means a 500-page PDF stalls crawl throughput.

---

## 3. Public interface

```python
class TaskService:
    async def enqueue(self, session: AsyncSession, task: TaskSpec) -> int:
        """MUST be called inside the caller's transaction (NFR-R-03).
        Signature takes a session deliberately — there is no session-less overload."""
    async def enqueue_many(self, session, tasks: Sequence[TaskSpec]) -> list[int]: ...
    async def cancel(self, task_id: int) -> bool: ...
    async def cancel_for_document(self, document_id: UUID) -> int: ...
    async def get_status(self, task_id: int) -> TaskStatus: ...
    async def queue_depth(self, queue: str | None = None) -> Mapping[str, int]: ...
    async def document_progress(self, document_id: UUID) -> DocumentTaskProgress: ...

class TaskWorker:
    def register(self, kind: str, handler: TaskHandler) -> None: ...
    async def run(self, queue: str, *, concurrency: int) -> None: ...

TaskHandler = Callable[[TaskContext], Awaitable[TaskResult]]

@dataclass
class TaskContext:
    task_id: int; workspace_id: UUID; kb_id: UUID | None
    document_id: UUID | None; payload: Mapping[str, Any]
    attempt: int; correlation_id: str | None
    async def heartbeat(self, extend: timedelta | None = None) -> None: ...
    async def report_progress(self, done: int, total: int) -> None: ...
```

---

## 4. Behaviour

### 4.1 Transactional enqueue (`NFR-R-03`) — the reason this module exists

```python
# ✅ CORRECT — one transaction, one atomic outcome
async with transaction() as session:
    doc = await catalog.create_document(session, ...)
    await tasks.enqueue(session, TaskSpec(queue="parse", document_id=doc.id, ...))
    # commit: either both exist or neither does

# ❌ IMPOSSIBLE BY DESIGN — enqueue() has no session-less form
await tasks.enqueue_without_session(...)
```

With a broker, a crash between the document commit and the broker publish leaves a document in
`registered` forever, with no task, no error, and nothing that can detect it. That failure mode
is structurally impossible here.

### 4.2 Claiming with fair scheduling

```sql
WITH candidates AS (
  SELECT eligible.id
    FROM workspace_runtime wr
    CROSS JOIN LATERAL (
      SELECT queued.id
        FROM task queued
       WHERE queued.workspace_id = wr.workspace_id
         AND queued.queue = :queue AND queued.state = 'ready'
         AND queued.run_after <= now()
       ORDER BY queued.priority DESC, queued.id
       LIMIT GREATEST(0, wr.concurrency_limit - wr.running)
    ) eligible
   WHERE wr.running < wr.concurrency_limit
), picked AS (
  SELECT t.id
    FROM task t
    JOIN candidates candidate ON candidate.id = t.id
   WHERE t.queue = :queue
     AND t.state = 'ready'
     AND t.run_after <= now()
   ORDER BY t.priority DESC, t.id                 -- FIFO within a priority band
   LIMIT :batch
     FOR UPDATE OF t SKIP LOCKED
)
UPDATE task t
   SET state = 'running', attempt = t.attempt + 1,
       worker_id = :worker_id, started_at = now(),
       lease_until = now() + :lease
  FROM picked p
 WHERE t.id = p.id
RETURNING t.*;
```

Then, in the same transaction:
`UPDATE workspace_runtime SET running = running + <n> WHERE workspace_id = ANY(...)`.

> **Why the counter table rather than a correlated subquery.** Counting running tasks per
> workspace inline is a subquery executed per candidate row — at 100k queued tasks it dominates
> the claim. A maintained counter bounds each workspace's candidate set. Without fairness, one
> workspace uploading 50k documents blocks every other workspace behind it, which is the
> defining failure of naive FIFO queues in multi-tenant systems.

`SKIP LOCKED` gives exactly-once claiming with no coordinator and no distributed lock.

The service sorts returned rows by descending priority, then id: SQL `RETURNING`
does not promise the CTE's ordering. Counters are updated in stable workspace-id
order to avoid lock-order inversions between workers. The candidate limit uses
remaining workspace capacity rather than batch size so a locked batch prefix
does not hide later eligible tasks. Caps remain soft across concurrent claim
transactions; strict global admission would require additional serialization.

Wakeups use `LISTEN/NOTIFY` on `cairn_task_{queue}`, with a 1 s poll as the safety net — so
latency is not bounded by the poll interval, but a missed notification is not fatal.

### 4.3 Leases, heartbeats, and reaping (`NFR-R-04`)

Long tasks extend their lease:

```python
async def handle_ocr(ctx: TaskContext) -> TaskResult:
    for i, page in enumerate(pages):
        await ocr_page(page)
        if i % 10 == 0:
            await ctx.heartbeat()              # extends lease_until
            await ctx.report_progress(i, len(pages))
```

The reaper (in `worker-maintain`, every 30 s):

```sql
UPDATE task
   SET state = 'ready', lease_until = NULL, worker_id = NULL,
       error_code = 'LEASE_EXPIRED'
 WHERE state = 'running' AND lease_until < now()
RETURNING id, workspace_id;
-- then decrement workspace_runtime.running for each
```

An OOM-killed worker's task returns to the queue within one reaper cycle. No lost work, no
manual intervention. Because handlers are idempotent (`NFR-R-06`), reprocessing is safe.

### 4.4 Retries and dead-lettering

```python
RETRYABLE = {"UPSTREAM_UNAVAILABLE","RATE_LIMIT_EXCEEDED","LEASE_EXPIRED",
             "CONNECTION_ERROR","TIMEOUT"}
TERMINAL  = {"UNSUPPORTED_FILE_TYPE","PARSE_ENCRYPTED_PDF","VALIDATION_FAILED",
             "QUOTA_EXCEEDED","PERMISSION_DENIED","EMBEDDING_DIMENSION_MISMATCH"}

def next_delay(attempt: int) -> timedelta:
    base = min(2 ** attempt, 300)                    # 2,4,8,…,300 s
    return timedelta(seconds=base * (0.5 + secrets.randbelow(1000) / 1000))  # jitter
```

Terminal errors go straight to `failed` rather than burning five attempts on a corrupt file —
which otherwise wastes ~10 minutes of queue capacity per bad document and delays every good one
behind it.

`failed` tasks are retryable by an operator (`POST /v1/documents/{id}/retry`), which resets
`attempt` to 0.

### 4.5 Batch claiming for the embed queue

`worker-embed` claims `LIMIT 64` and groups by `(kb_id, embedding_model)` to fill GPU batches.
A batch is one dynamic unit: partial failure marks only the affected tasks failed, so one bad
chunk does not fail 63 good ones.

### 4.6 Deduplication

`dedupe_key` with a partial unique index on `(dedupe_key) WHERE state IN ('ready','running')`
prevents duplicate in-flight work. Enqueue uses `ON CONFLICT DO NOTHING` and returns the
existing task's ID. Convention: `{kind}:{entity_id}:{revision}`.

### 4.7 Worker harness

```python
async def run(self, queue: str, *, concurrency: int) -> None:
    sem = asyncio.Semaphore(concurrency)
    async with self._listen(queue):
        while not self._shutdown.is_set():
            batch = await self._claim(queue, batch=min(concurrency, 64))
            if not batch:
                await self._wait_for_notify_or(timeout=1.0)
                continue
            for task in batch:
                asyncio.create_task(self._execute(task, sem))
        await self._drain(timeout=self.settings.graceful_shutdown_s)
```

Graceful shutdown on SIGTERM: stop claiming, let in-flight tasks finish (default 30 s), then
release leases explicitly so work is reclaimed immediately rather than after lease expiry.

Every task execution: opens a trace span linked to `correlation_id`, binds log context, records
`cairn_task_duration_seconds`, and updates `workspace_runtime.running` on completion.

### 4.8 Metrics (`NFR-S-06`)

```
cairn_task_queue_depth{queue}                  gauge      ← KEDA scales on this
cairn_task_oldest_ready_age_seconds{queue}     gauge      ← the real starvation signal
cairn_task_running{queue}                      gauge
cairn_task_duration_seconds{queue,kind,outcome} histogram
cairn_task_attempts_total{queue,kind}          counter
cairn_task_lease_expired_total{queue}          counter    ← alert if non-zero and rising
cairn_task_dead_lettered_total{queue,error_code} counter
```

> Autoscale on **backlog depth**, not CPU. A queue consumer's CPU is a lagging indicator — it
> only rises after work is already delayed, so CPU-based scaling always responds one task
> duration too late.

### 4.9 Maintenance duties

`worker-maintain` also runs, on schedule: lease reaping (30 s), pruning `done` tasks older than
7 days (hourly, batched), creating `audit_log`/`usage_record` partitions two months ahead
(daily), expiring sessions (hourly), dropping retired index namespaces past `retire_after`
(hourly), and read-model reconciliation (nightly, ADR-0005).

---

## 5. Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `CAIRN_TASKS__QUEUE` | — | Which queue this worker serves (worker role) |
| `CAIRN_TASKS__CONCURRENCY` | per-queue default | |
| `CAIRN_TASKS__CLAIM_BATCH` | `16` | `64` for `embed` |
| `CAIRN_TASKS__LEASE_SECONDS` | per-queue | |
| `CAIRN_TASKS__POLL_INTERVAL_S` | `1.0` | Safety net under LISTEN/NOTIFY |
| `CAIRN_TASKS__REAPER_INTERVAL_S` | `30` | |
| `CAIRN_TASKS__MAX_ATTEMPTS` | `5` | |
| `CAIRN_TASKS__GRACEFUL_SHUTDOWN_S` | `30` | |
| `CAIRN_TASKS__DEFAULT_WS_CONCURRENCY` | `16` | Per-workspace fairness cap |
| `CAIRN_TASKS__RETAIN_DONE_DAYS` | `7` | |

---

## 6. Performance requirements

| Operation | Budget |
| --- | --- |
| `enqueue` (within an existing transaction) | < 2 ms |
| `claim` batch of 16 | < 10 ms p95 at 100k ready tasks |
| Claim throughput | ≥ 1000 tasks/s per worker |
| Notify-to-claim latency | < 100 ms p95 |
| Reaper pass | < 500 ms |
| `queue_depth` | < 20 ms (partial index count) |

---

## 7. Test requirements

| ID | Test | Req |
| --- | --- | --- |
| TC-M06-01 | Enqueue rolls back with its transaction | NFR-R-03 |
| TC-M06-02 | **No API allows enqueue outside a transaction** | NFR-R-03 |
| TC-M06-03 | **10 concurrent workers claim 1000 tasks with zero duplicates** | — |
| TC-M06-04 | Claim respects `priority DESC, id` | — |
| TC-M06-05 | Claim skips tasks with `run_after` in the future | — |
| TC-M06-06 | **Workspace at its concurrency cap is skipped; others proceed** | — |
| TC-M06-07 | **Fairness: workspace A with 10k tasks does not starve workspace B** | — |
| TC-M06-08 | **Killed worker's task is reclaimed within 2× the lease** | NFR-R-04 |
| TC-M06-09 | Heartbeat extends the lease; the reaper leaves it alone | NFR-R-04 |
| TC-M06-10 | Retryable errors back off exponentially with jitter | — |
| TC-M06-11 | Terminal errors fail immediately without retrying | — |
| TC-M06-12 | `max_attempts` exhaustion dead-letters with the last error | — |
| TC-M06-13 | `dedupe_key` prevents a duplicate in-flight task | — |
| TC-M06-14 | Same `dedupe_key` is allowed again after completion | — |
| TC-M06-15 | Cancellation of a `ready` task; a `running` task finishes | — |
| TC-M06-16 | SIGTERM drains in-flight work and releases leases | — |
| TC-M06-17 | Batch claim groups embed tasks by KB and model | — |
| TC-M06-18 | Partial batch failure fails only the affected tasks | — |
| TC-M06-19 | Benchmark: 1000 claims/s with 100k ready tasks | — |
| TC-M06-20 | `queue_depth` matches actual counts under concurrent mutation | NFR-S-06 |
| TC-M06-21 | Pruning removes only `done` tasks past retention | — |
| TC-M06-22 | Progress reporting surfaces through `document_progress` | FR-G-09 |

Coverage target: **90%**.

---

## 8. Acceptance criteria

- [ ] All 22 test cases pass
- [ ] TC-M06-03 passes 100 consecutive runs (no flakiness in the claim path)
- [ ] Benchmark TC-M06-19 met
- [ ] No code path can enqueue outside a transaction (CI check on the signature)
- [ ] Chaos test: kill workers randomly during a 1000-document ingest; every document completes
- [ ] Metrics exported and a KEDA scaler configuration verified against them

---

## 9. Task breakdown

| Task | Description | Est (d) | Deps |
| --- | --- | --- | --- |
| T-M06-01 | ORM + migration: `task`, `workspace_runtime`, partial indexes | 0.5 | T-M00-05 |
| T-M06-02 | `TaskSpec`/`TaskContext`/`TaskResult` types | 0.5 | |
| T-M06-03 | **Transactional enqueue + dedupe** | 1.0 | T-M06-01 |
| T-M06-04 | **Claim query with fairness join** | 1.5 | T-M06-01 |
| T-M06-05 | Lease, heartbeat, reaper | 1.0 | T-M06-04 |
| T-M06-06 | Retry classification, backoff, dead-letter | 1.0 | T-M06-04 |
| T-M06-07 | Worker harness, LISTEN/NOTIFY, graceful shutdown | 1.5 | T-M06-04 |
| T-M06-08 | Batch claiming for `embed` | 0.5 | T-M06-07 |
| T-M06-09 | Progress reporting + `document_progress` | 0.5 | T-M06-07 |
| T-M06-10 | Metrics + tracing integration | 0.5 | T-M06-07 |
| T-M06-11 | Maintenance jobs: prune, partitions, sessions, namespaces | 1.0 | T-M06-07 |
| T-M06-12 | Cancellation | 0.5 | T-M06-03 |
| T-M06-13 | Tests TC-M06-01..22 + chaos + benchmark | 2.5 | all |
| | **Total** | **12.5** | |
