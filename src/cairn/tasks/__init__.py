"""Durable task queue.

Background work lives in a PostgreSQL table, not a broker (ADR-0003). The
decisive property is **transactional enqueue**: the row that needs work and the
task that does it commit together, so the "document exists but was never
processed" failure class is structurally impossible rather than merely rare.

Secondary consequences, all of which we want anyway: job state is SQL-queryable
(the per-document progress UI is a SELECT), fair scheduling is an ORDER BY, and
retries and dead-lettering are UPDATEs.
"""

from cairn.tasks.dto import QueueName, TaskContext, TaskResult, TaskSpec, TaskStatus
from cairn.tasks.service import TaskService, get_task_service
from cairn.tasks.worker import TaskHandler, TaskWorker

__all__ = [
    "QueueName",
    "TaskContext",
    "TaskHandler",
    "TaskResult",
    "TaskService",
    "TaskSpec",
    "TaskStatus",
    "TaskWorker",
    "get_task_service",
]
